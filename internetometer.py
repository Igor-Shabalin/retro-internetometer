#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Интернетометр
=============

Измеряет скорость интернета, показывает её стрелочным вольтметром через ШИМ
Raspberry Pi, зажигает светофор из трёх светодиодов и ведёт веб-журнал
с графиком-самописцем.

Зависимости: только стандартная библиотека Python + gpiozero (на Raspberry Pi).
Без Raspberry Pi запускается в режиме эмуляции — интерфейс полностью работает.

    python3 internetometer.py                 # обычный запуск
    python3 internetometer.py --test-once     # один замер и выход
    python3 internetometer.py --sweep         # прогон стрелки (проверка железа)
    python3 internetometer.py --set-volts 1.5 # подать 1,5 В (калибровка)
    python3 internetometer.py --no-gpio       # эмуляция, без железа
"""

import argparse
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

APP_VERSION = "1.0"

# --------------------------------------------------------------------------
# Настройки по умолчанию. Всё, что есть в config.json, перекрывает их.
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "interval_minutes": 5,
    "http_port": 8080,
    "http_host": "0.0.0.0",
    "database": "internetometer.db",
    "title": "Интернетометр",
    "subtitle": "Вольтметр 1940 года на службе у оптоволокна",

    "meter": {
        "gpio_pwm": 18,
        "pwm_frequency_hz": 1000,
        "supply_voltage": 3.3,
        "full_scale_voltage": 3.0,
        "full_scale_mbps": 1000,
        "scale_divisions": 150,
        "scale": "linear",
        "source": "download",
        "calibration_gain": 1.0,
        "zero_duty": 0.0,
        "max_duty": 0.98,
        "needle_ramp_seconds": 2.5,
        "sweep_on_start": True,
        "source_interface": None
    },

    "leds": {
        "green_gpio": 17,
        "yellow_gpio": 27,
        "red_gpio": 22,
        "activity_gpio": 23,
        "active_high": True,
        "green_min_mbps": 300,
        "yellow_min_mbps": 100
    },

    "interfaces": [],

    "yandex": {
        "binary": "internetometer",
        "args": ["--all", "--json", "--lang", "ru"],
        "concurrency": 4,
        "timeout_seconds": 240,
        "custom_command": ""
    }
}


def deep_merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path=CONFIG_PATH):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        return deep_merge(DEFAULT_CONFIG, user_cfg)
    return dict(DEFAULT_CONFIG)


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def clamp(value, low, high):
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Железо
# --------------------------------------------------------------------------

class _FakePWM:
    """Заглушка на случай запуска не на Raspberry Pi."""

    def __init__(self, *_a, **_kw):
        self.value = 0.0

    def close(self):
        self.value = 0.0


class _FakeLED:
    def __init__(self, *_a, **_kw):
        self.value = 0

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0

    def close(self):
        self.value = 0


def open_gpio(cfg, enabled=True):
    """Возвращает (класс_ШИМ, класс_светодиода, есть_ли_железо)."""
    if not enabled:
        return _FakePWM, _FakeLED, False
    try:
        from gpiozero import DigitalOutputDevice, PWMOutputDevice
        return PWMOutputDevice, DigitalOutputDevice, True
    except Exception as exc:  # noqa: BLE001
        log(f"gpiozero недоступен ({exc}). Работаю в режиме эмуляции.")
        return _FakePWM, _FakeLED, False


class Meter:
    """
    Стрелка: держит целевое напряжение и плавно подводит к нему реальное,
    чтобы стрелка не билась об упоры.
    """

    def __init__(self, cfg, use_gpio=True):
        self.cfg = cfg["meter"]
        self.leds_cfg = cfg["leds"]
        pwm_cls, led_cls, self.real = open_gpio(cfg, use_gpio)

        self.vcc = float(self.cfg["supply_voltage"])
        self.v_full = float(self.cfg["full_scale_voltage"])
        self.gain = float(self.cfg["calibration_gain"])
        self.zero_duty = float(self.cfg["zero_duty"])
        self.max_duty = float(self.cfg["max_duty"])
        self.ramp_seconds = float(self.cfg["needle_ramp_seconds"])

        if self.real:
            self.pwm = pwm_cls(
                self.cfg["gpio_pwm"],
                frequency=int(self.cfg["pwm_frequency_hz"]),
                initial_value=0.0,
            )
        else:
            self.pwm = pwm_cls()

        active_high = bool(self.leds_cfg.get("active_high", True))
        self.lamps = {}
        for name, key in (
            ("green", "green_gpio"),
            ("yellow", "yellow_gpio"),
            ("red", "red_gpio"),
            ("activity", "activity_gpio"),
        ):
            pin = self.leds_cfg.get(key)
            if pin is None:
                self.lamps[name] = _FakeLED()
                continue
            if self.real:
                self.lamps[name] = led_cls(pin, active_high=active_high, initial_value=False)
            else:
                self.lamps[name] = led_cls()

        self._lock = threading.Lock()
        self._target_v = 0.0
        self._current_v = 0.0
        self._manual = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._ramp_loop, daemon=True)
        self._thread.start()

    # -- пересчёт -------------------------------------------------------

    def speed_to_voltage(self, mbps):
        full = float(self.cfg["full_scale_mbps"])
        mode = self.cfg.get("scale", "linear")
        value = max(0.0, float(mbps or 0.0))
        if mode == "log":
            frac = math.log10(1.0 + 9.0 * min(value, full) / full)
        elif mode == "sqrt":
            frac = math.sqrt(min(value, full) / full)
        else:
            frac = min(value, full) / full
        return clamp(frac, 0.0, 1.0) * self.v_full

    def voltage_to_divisions(self, volts):
        divisions = float(self.cfg.get("scale_divisions", 150))
        return clamp(volts / self.v_full, 0.0, 1.0) * divisions

    def _voltage_to_duty(self, volts):
        duty = (volts / self.vcc) * self.gain
        if volts > 0.001:
            duty += self.zero_duty
        return clamp(duty, 0.0, self.max_duty)

    # -- управление -----------------------------------------------------

    def set_voltage(self, volts, immediate=False, manual=False):
        volts = clamp(float(volts), 0.0, self.v_full)
        with self._lock:
            self._manual = manual
            self._target_v = volts
            if immediate:
                self._current_v = volts
                self.pwm.value = self._voltage_to_duty(volts)

    def set_speed(self, mbps):
        if self._manual:
            return
        self.set_voltage(self.speed_to_voltage(mbps))

    def release_manual(self):
        with self._lock:
            self._manual = False

    def _ramp_loop(self):
        tick = 0.02
        while not self._stop.is_set():
            with self._lock:
                target = self._target_v
                current = self._current_v
                if self.ramp_seconds <= 0:
                    current = target
                else:
                    step = self.v_full * tick / self.ramp_seconds
                    if abs(target - current) <= step:
                        current = target
                    else:
                        current += step if target > current else -step
                if abs(current - self._current_v) > 1e-6 or current != target:
                    self._current_v = current
                    self.pwm.value = self._voltage_to_duty(current)
            time.sleep(tick)

    def sweep(self):
        """Прогон стрелки от нуля до упора и обратно — как при включении прибора."""
        log("Прогон стрелки")
        saved = self._target_v
        self.set_voltage(self.v_full)
        time.sleep(self.ramp_seconds + 0.6)
        self.set_voltage(0.0)
        time.sleep(self.ramp_seconds + 0.4)
        self.set_voltage(saved)

    # -- лампы ----------------------------------------------------------

    def zone_for(self, mbps):
        if mbps is None:
            return "off"
        if mbps >= float(self.leds_cfg["green_min_mbps"]):
            return "green"
        if mbps >= float(self.leds_cfg["yellow_min_mbps"]):
            return "yellow"
        return "red"

    def set_zone(self, zone):
        for name in ("green", "yellow", "red"):
            if zone == name:
                self.lamps[name].on()
            else:
                self.lamps[name].off()

    def activity(self, on):
        if on:
            self.lamps["activity"].on()
        else:
            self.lamps["activity"].off()

    # -- состояние / выключение ----------------------------------------

    def snapshot(self):
        with self._lock:
            current = self._current_v
            target = self._target_v
            manual = self._manual
        return {
            "voltage": round(current, 4),
            "target_voltage": round(target, 4),
            "duty": round(self._voltage_to_duty(current), 4),
            "divisions": round(self.voltage_to_divisions(current), 2),
            "target_divisions": round(self.voltage_to_divisions(target), 2),
            "full_scale_voltage": self.v_full,
            "full_scale_mbps": self.cfg["full_scale_mbps"],
            "scale_divisions": self.cfg.get("scale_divisions", 150),
            "scale": self.cfg.get("scale", "linear"),
            "manual": manual,
            "hardware": self.real,
        }

    def shutdown(self):
        self.set_voltage(0.0)
        deadline = time.time() + self.ramp_seconds + 1.0
        while time.time() < deadline and self._current_v > 0.01:
            time.sleep(0.05)
        self._stop.set()
        try:
            self.pwm.value = 0.0
            self.pwm.close()
        except Exception:  # noqa: BLE001
            pass
        for lamp in self.lamps.values():
            try:
                lamp.off()
                lamp.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# Беспроводные данные: уровень сигнала, канал и его загруженность
# --------------------------------------------------------------------------

def channel_from_freq(freq):
    """МГц → номер канала Wi-Fi."""
    if not freq:
        return None
    freq = int(freq)
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5160 <= freq <= 5885:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:          # диапазон 6 ГГц
        return (freq - 5950) // 5
    return None


def band_from_freq(freq):
    if not freq:
        return None
    freq = int(freq)
    if freq < 3000:
        return "2.4"
    if freq < 5900:
        return "5"
    return "6"


def _run_quiet(cmd, timeout=6):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout
    except Exception:  # noqa: BLE001
        return ""


def wireless_link(iface):
    """Разбирает `iw dev <iface> link`. Для проводного интерфейса вернёт пустой словарь."""
    out = _run_quiet(["iw", "dev", iface, "link"])
    if not out or "Not connected" in out:
        return {}
    info = {}
    ssid = re.search(r"^\s*SSID:\s*(.+)$", out, re.M)
    freq = re.search(r"^\s*freq:\s*(\d+)", out, re.M)
    signal = re.search(r"^\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", out, re.M)
    tx = re.search(r"^\s*tx bitrate:\s*([\d.]+)\s*([MG])Bit/s", out, re.M)
    rx = re.search(r"^\s*rx bitrate:\s*([\d.]+)\s*([MG])Bit/s", out, re.M)
    if ssid:
        info["ssid"] = ssid.group(1).strip()
    if freq:
        info["freq"] = int(freq.group(1))
        info["channel"] = channel_from_freq(info["freq"])
        info["band"] = band_from_freq(info["freq"])
    if signal:
        info["signal"] = float(signal.group(1))
    for match, key in ((tx, "tx_rate"), (rx, "rx_rate")):
        if match:
            value = float(match.group(1))
            info[key] = value * 1000 if match.group(2) == "G" else value
    return info


def wireless_survey(iface):
    """Счётчики занятости канала из `iw dev <iface> survey dump` для текущего канала."""
    out = _run_quiet(["iw", "dev", iface, "survey", "dump"])
    if not out:
        return {}
    for block in out.split("Survey data from")[1:]:
        if "[in use]" not in block:
            continue
        active = re.search(r"channel active time:\s*(\d+)", block)
        busy = re.search(r"channel busy time:\s*(\d+)", block)
        noise = re.search(r"noise:\s*(-?\d+)", block)
        result = {}
        if active:
            result["busy_active"] = int(active.group(1))
        if busy:
            result["busy_total"] = int(busy.group(1))
        if noise:
            result["noise"] = float(noise.group(1))
        return result
    return {}


def wifi_neighbours(iface, own_freq=None):
    """
    Считает чужие сети в том же диапазоне.

    Замена занятости эфира для драйверов, которые не поддерживают
    `iw survey dump` (например, проприетарный Broadcom wl). Берёт кэш
    сканирования, своим сканированием связь не рвёт.
    """
    freqs = []

    out = _run_quiet(["nmcli", "-t", "-f", "SSID,FREQ", "device", "wifi", "list",
                      "--rescan", "no"], timeout=10)
    for line in (out or "").splitlines():
        match = re.search(r"(\d{4})\s*(?:MHz|МГц)", line)
        if match:
            freqs.append(int(match.group(1)))

    if not freqs:
        out = _run_quiet(["iw", "dev", iface, "scan", "dump"], timeout=12)
        freqs = [int(m) for m in re.findall(r"^\s*freq:\s*(\d+)", out or "", re.M)]

    if not freqs:
        return {}

    own_band = band_from_freq(own_freq) if own_freq else None
    own_channel = channel_from_freq(own_freq) if own_freq else None

    same_band = [f for f in freqs if band_from_freq(f) == own_band] if own_band else freqs
    # На 2,4 ГГц каналы перекрываются: мешают все в пределах ±4 номеров.
    # На 5 ГГц полосы не налезают друг на друга, считаем только точное совпадение.
    if own_channel is not None:
        span = 4 if own_band == "2.4" else 0
        near = [f for f in same_band
                if channel_from_freq(f) is not None
                and abs(channel_from_freq(f) - own_channel) <= span]
    else:
        near = []

    return {
        "neighbours": max(0, len(same_band) - 1),
        "neighbours_near": max(0, len(near) - 1),
    }


class WirelessWatcher:
    """
    Считает занятость канала по приращению счётчиков `iw survey`.

    Важная тонкость: во время самого замера скорости мы сами занимаем эфир почти
    полностью, и занятость покажет 90 % на любом канале. Поэтому занятость снимается
    отдельным коротким окном ДО замера, когда в эфире только соседи.
    """

    def __init__(self):
        self._previous = {}

    def busy_window(self, iface, seconds=8):
        """Занятость эфира за короткое окно тишины перед замером."""
        first = wireless_survey(iface)
        if "busy_active" not in first:
            return {}
        time.sleep(max(2, seconds))
        second = wireless_survey(iface)
        if "busy_active" not in second:
            return {}
        d_active = second["busy_active"] - first["busy_active"]
        d_busy = second["busy_total"] - first["busy_total"]
        out = {k: v for k, v in second.items() if k in ("noise", "busy_active", "busy_total")}
        if d_active > 0 and d_busy >= 0:
            out["busy"] = round(min(100.0, d_busy / d_active * 100), 1)
        self._previous[iface] = (second["busy_active"], second["busy_total"])
        return out

    def neighbours(self, iface, freq):
        """Считает соседние сети. Только ПОСЛЕ замера: чтение списка сетей
        может спровоцировать сканирование, а оно рвёт связь на несколько секунд."""
        return wifi_neighbours(iface, freq)

    def sample(self, iface):
        info = wireless_link(iface)
        survey = wireless_survey(iface)
        info.update(survey)

        active, busy = survey.get("busy_active"), survey.get("busy_total")
        if active is not None and busy is not None:
            previous = self._previous.get(iface)
            if previous:
                d_active = active - previous[0]
                d_busy = busy - previous[1]
                if d_active > 0 and d_busy >= 0:
                    info["busy"] = round(min(100.0, d_busy / d_active * 100), 1)
            if "busy" not in info and active > 0:
                info["busy"] = round(min(100.0, busy / active * 100), 1)
            self._previous[iface] = (active, busy)

        return info


def interface_address(iface):
    """IPv4-адрес интерфейса, нужен для проверки готовности канала."""
    out = _run_quiet(["ip", "-4", "-o", "addr", "show", "dev", iface])
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out or "")
    return match.group(1) if match else None


def _iface_counters():
    """Принятые байты по каждому интерфейсу: {'eth0': 12345, 'wlan0': 678}."""
    counters = {}
    base = "/sys/class/net"
    try:
        names = os.listdir(base)
    except OSError:
        return counters
    for name in names:
        if name == "lo":
            continue
        try:
            with open(f"{base}/{name}/statistics/rx_bytes", encoding="ascii") as handle:
                counters[name] = int(handle.read().strip())
        except (OSError, ValueError):
            continue
    return counters


def _traffic_carrier(before, after, floor_bytes=5_000_000):
    """
    Определяет, какой интерфейс реально принял трафик замера, и его долю.

    Нужно, чтобы поймать утечку: маршрут может выглядеть правильным, а трафик
    всё равно уйти через другой интерфейс (например, по IPv6, у которого свой
    маршрут по умолчанию, или из-за политики маршрутизации). Возвращает
    (имя_интерфейса, доля) либо (None, 0.0), если трафика было слишком мало,
    чтобы судить.
    """
    deltas = {}
    for name, value in after.items():
        delta = value - before.get(name, value)
        if delta > 0:
            deltas[name] = delta
    total = sum(deltas.values())
    if total < floor_bytes:
        return None, 0.0
    winner = max(deltas, key=deltas.get)
    return winner, deltas[winner] / total


def interface_gateway(iface):
    """Ищет шлюз интерфейса. NetworkManager не всегда держит для него
    маршрут по умолчанию в главной таблице, поэтому пробуем три источника."""
    for command in (
        ["ip", "-4", "route", "show", "default", "dev", iface],
        ["ip", "-4", "route", "show", "table", "all", "default", "dev", iface],
    ):
        match = re.search(r"via\s+(\d+\.\d+\.\d+\.\d+)", _run_quiet(command) or "")
        if match:
            return match.group(1)

    out = _run_quiet(["nmcli", "-g", "IP4.GATEWAY", "device", "show", iface])
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", out or "")
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Замер скорости через Яндекс Интернетометр
# --------------------------------------------------------------------------

def _norm_key(key):
    return re.sub(r"[^a-zа-я0-9]", "", str(key).lower())


def _walk(data, prefix=""):
    """Разворачивает вложенный JSON в плоский список пар (исходный_ключ, значение)."""
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                yield from _walk(v, path)
            else:
                yield path, v
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from _walk(v, f"{prefix}[{i}]")


def _to_mbps(key, value):
    """Приводит скорость к мегабитам в секунду, ориентируясь на имя поля."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    k = _norm_key(key)
    if "мбит" in k or "mbit" in k or "mbps" in k or "megabit" in k:
        return value
    if "кбит" in k or "kbit" in k or "kbps" in k:
        return value / 1000.0
    if "byte" in k or "байт" in k:
        return value * 8 / 1e6
    if "bps" in k or "bit" in k or "бит" in k:
        return value / 1e6
    # Имя ничего не подсказывает — считаем по порядку величины.
    # Домашний канал быстрее 5 Гбит/с маловероятен, значит крупные числа — это не Мбит/с.
    if value > 5_000_000:
        return value / 1e6
    if value > 5_000:
        return value / 1000.0
    return value


DOWN_KEYS = ("downloadmbps", "download", "downloadspeed", "speeddownload", "incoming",
             "in", "rx", "downlink", "скачивание", "входящая", "вход")
UP_KEYS = ("uploadmbps", "upload", "uploadspeed", "speedupload", "outgoing",
           "out", "tx", "uplink", "отдача", "исходящая", "исход")
PING_KEYS = ("latencyms", "latency", "pingms", "ping", "rtt", "задержка", "пинг")
JITTER_KEYS = ("jitterms", "jitter", "джиттер")
IPV4_KEYS = ("ipv4", "ip4", "ip", "address")
IPV6_KEYS = ("ipv6", "ip6")
REGION_KEYS = ("region", "regionname", "city", "регион", "город")
ISP_KEYS = ("isp", "provider", "org", "operator", "провайдер")
ASN_KEYS = ("asn", "as", "autonomoussystem", "asnumber")


def _pick(flat, wanted, numeric=True):
    """Ищет первое поле, чьё нормализованное имя совпадает или заканчивается на искомое."""
    for exact in (True, False):
        for name in wanted:
            for key, value in flat:
                k = _norm_key(key.split(".")[-1])
                hit = (k == name) if exact else k.endswith(name)
                if not hit:
                    continue
                if numeric:
                    try:
                        float(value)
                    except (TypeError, ValueError):
                        continue
                    return key, value
                if value not in (None, "", "-"):
                    return key, value
    return None, None


SPEED_RE = re.compile(
    r"(-?\d+(?:[.,]\d+)?)\s*([KMGkmgКМГкмг]?)\s*(bit/s|bits|bit|bytes|byte|b|B|бит|байт)",
)
NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
PAREN_RE = re.compile(r"\(([^)]*)\)")
ASN_RE = re.compile(r"\bAS\s*(\d+)\b", re.I)

_PREFIX = {"": 1.0, "k": 1e3, "к": 1e3, "m": 1e6, "м": 1e6, "g": 1e9, "г": 1e9}


def _speed_text_to_mbps(text):
    """«91.04 Mbps», «11,5 МБит/с», «1.2 GB/s» → мегабиты в секунду."""
    match = SPEED_RE.search(text or "")
    if not match:
        number = NUMBER_RE.search(text or "")
        return float(number.group(0).replace(",", ".")) if number else None
    value = float(match.group(1).replace(",", "."))
    bits = value * _PREFIX.get(match.group(2).lower(), 1.0)
    unit = match.group(3)
    if unit in ("B", "byte", "bytes", "байт"):
        bits *= 8
    return bits / 1e6


def _plain_number(text):
    match = NUMBER_RE.search(text or "")
    return float(match.group(0).replace(",", ".")) if match else None


class Measurer:
    """Обёртка над internetometer-cli — консольным клиентом Яндекс Интернетометра."""

    def __init__(self, cfg):
        self.cfg = cfg.get("yandex") or cfg.get("speedtest") or {}
        self.timeout = int(self.cfg.get("timeout_seconds", 240))
        self.binary = self._locate()
        self.mapping = {}
        log(f"Клиент Яндекс Интернетометра: {self.binary or 'не найден'}")

    def _locate(self):
        custom = (self.cfg.get("custom_command") or "").strip()
        if custom:
            return custom.split()[0]
        name = self.cfg.get("binary", "internetometer")
        if os.path.sep in name:
            return name if os.path.exists(name) else None
        found = shutil.which(name)
        if found:
            return found
        for candidate in (
            os.path.join(BASE_DIR, name),
            os.path.expanduser(f"~/internetometer-cli/{name}"),
            f"/usr/local/bin/{name}",
        ):
            if os.path.exists(candidate):
                return candidate
        return None

    def command(self, link=None):
        custom = (self.cfg.get("custom_command") or "").strip()
        if custom:
            cmd = custom.split()
        elif self.binary:
            cmd = [self.binary] + list(self.cfg.get("args", ["--all", "--json", "--lang", "ru"]))
            concurrency = self.cfg.get("concurrency")
            if concurrency and "--concurrency" not in cmd:
                cmd += ["--concurrency", str(concurrency)]
        else:
            return []
        return (link.prefix() if link else []) + cmd

    @staticmethod
    def extract_json(text):
        """Достаёт JSON из вывода: клиент умеет подмешивать рамки TUI и цвета."""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "").strip()
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = None
        raise ValueError("в выводе клиента не нашёлся JSON — проверьте флаг --json")

    def parse(self, data):
        flat = list(_walk(data))
        mapping = {}

        def number(keys, label, convert=None):
            key, value = _pick(flat, keys)
            if key is None:
                return None
            mapping[label] = key
            return convert(key, value) if convert else float(value)

        def text(keys, label):
            key, value = _pick(flat, keys, numeric=False)
            if key is None:
                return None
            mapping[label] = key
            return str(value)

        download = number(DOWN_KEYS, "download", _to_mbps)
        upload = number(UP_KEYS, "upload", _to_mbps)
        if download is None:
            raise ValueError(
                "не нашёл скорость приёма в ответе клиента. "
                "Запустите: python3 internetometer.py --probe"
            )
        ping = number(PING_KEYS, "ping")
        jitter = number(JITTER_KEYS, "jitter")
        asn = text(ASN_KEYS, "asn")
        self.mapping = mapping

        return {
            "download": round(download, 2),
            "upload": round(upload, 2) if upload is not None else None,
            "ping": round(ping, 1) if ping is not None else None,
            "jitter": round(jitter, 1) if jitter is not None else None,
            "ipv4": text(IPV4_KEYS, "ipv4"),
            "ipv6": text(IPV6_KEYS, "ipv6"),
            "region": text(REGION_KEYS, "region"),
            "isp": text(ISP_KEYS, "isp"),
            "asn": f"AS{asn}" if asn and not str(asn).upper().startswith("AS") else asn,
            "node": text(("node", "host", "hostname", "server", "cdn"), "node"),
        }

    def parse_text(self, text):
        """Разбирает обычный вывод клиента вида «Download: 91.04 Mbps»."""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
        fields = {}
        for line in text.splitlines():
            match = re.match(r"\s*([^:]{2,40}?)\s*:\s*(\S.*?)\s*$", line)
            if match:
                fields.setdefault(_norm_key(match.group(1)), match.group(2))

        def grab(names):
            for name in names:
                for key, value in fields.items():
                    if key == name:
                        return value
            for name in names:
                for key, value in fields.items():
                    if key.startswith(name) or key.endswith(name):
                        return value
            return None

        download_raw = grab(("download", "скачивание", "входящая", "приём", "прием"))
        if download_raw is None:
            raise ValueError("в текстовом выводе нет строки Download")
        upload_raw = grab(("upload", "отдача", "исходящая"))

        region_raw = grab(("region", "регион")) or ""
        isp_raw = grab(("isp", "provider", "провайдер")) or ""
        node = None
        region = region_raw
        inside = PAREN_RE.search(region_raw)
        if inside:
            node = inside.group(1).strip()
            region = PAREN_RE.sub("", region_raw).strip()

        asn = None
        isp = isp_raw
        asn_match = ASN_RE.search(isp_raw)
        if asn_match:
            asn = "AS" + asn_match.group(1)
            isp = PAREN_RE.sub("", isp_raw).strip()

        latency = grab(("latency", "ping", "задержка", "пинг"))
        jitter = grab(("jitter", "джиттер"))

        self.mapping = {"источник": "текстовый вывод клиента"}
        return {
            "download": round(_speed_text_to_mbps(download_raw), 2),
            "upload": round(_speed_text_to_mbps(upload_raw), 2) if upload_raw else None,
            "ping": round(_plain_number(latency), 2) if latency else None,
            "jitter": round(_plain_number(jitter), 2) if jitter else None,
            "ipv4": grab(("ipv4", "ip4", "ip")),
            "ipv6": grab(("ipv6", "ip6")),
            "region": region or None,
            "isp": isp or None,
            "asn": asn,
            "node": node,
        }

    def raw(self, args=None, link=None):
        if args is not None:
            cmd = (link.prefix() if link else []) + [self.binary] + list(args)
        else:
            cmd = self.command(link)
        if not cmd or not cmd[0]:
            raise RuntimeError(
                "Не найден клиент Яндекс Интернетометра. Соберите internetometer-cli "
                "или укажите путь в config.json → yandex.binary"
            )
        self.last_command = cmd
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        output = proc.stdout if proc.stdout.strip() else proc.stderr
        if proc.returncode != 0 and not output.strip():
            raise RuntimeError((proc.stderr or "клиент завершился с ошибкой").strip()[:400])
        return output

    def measure(self, args=None, link=None):
        """Один прогон клиента: пробуем JSON, потом обычный текст."""
        output = self.raw(args, link)
        try:
            return self.parse(self.extract_json(output))
        except Exception as json_error:  # noqa: BLE001
            try:
                return self.parse_text(output)
            except Exception:  # noqa: BLE001
                raise json_error

    def run(self, link=None):
        started = time.time()
        try:
            result = self.measure(link=link)
        except Exception as first_error:  # noqa: BLE001
            # Флаги могли не подойти этой сборке клиента — пробуем без них.
            try:
                result = self.measure(args=[], link=link)
            except Exception:  # noqa: BLE001
                raise first_error
        result["duration"] = round(time.time() - started, 1)
        result.setdefault("node", None)
        result["server"] = result.get("node") or ", ".join(
            x for x in (result.get("region"), result.get("asn")) if x
        ) or "Яндекс Интернетометр"
        return result


# --------------------------------------------------------------------------
# Сетевые каналы: Ethernet и Wi-Fi по отдельности
# --------------------------------------------------------------------------

class Link:
    """
    Один измеряемый канал. Отвечает за то, чтобы клиент Яндекса вышел в сеть
    именно через свой интерфейс.

    Способы привязки:
      uid    — клиент запускается от отдельного пользователя, чей трафик
               правилом «ip rule uidrange» уходит в свою таблицу маршрутизации.
               Все интерфейсы при этом подняты одновременно. Ничего не ломает.
      switch — перед замером канал поднимается (для Wi-Fi — переподключение к
               нужной сети), и на время замера он становится маршрутом по
               умолчанию. Так можно обойтись одним радиомодулем: wlan0 по очереди
               ходит то на 5 ГГц, то на 2,4 ГГц.
      route  — интерфейс уже поднят, но на время замера временно подменяется
               маршрут по умолчанию.
      none   — ничего не делать, замер идёт обычным маршрутом.
    """

    def __init__(self, cfg, index=0):
        self.id = cfg.get("id") or cfg.get("iface") or f"link{index}"
        self.iface = cfg.get("iface") or self.id
        self.label = cfg.get("label") or self.iface
        self.mode = cfg.get("mode", "uid")
        self.user = cfg.get("user") or f"netm-{self.iface}"
        self.table = cfg.get("table") or (100 + index)
        self.color = cfg.get("color")
        self.drives_meter = bool(cfg.get("drives_meter", index == 0))
        self.wireless = bool(cfg.get("wireless", self.iface.startswith(("wlan", "wlp", "wlx"))))
        self.connection = cfg.get("connection")
        self.up_command = cfg.get("up_command")
        self.down_command = cfg.get("down_command")
        self.wait_seconds = int(cfg.get("wait_seconds", 45))
        self.settle_seconds = float(cfg.get("settle_seconds", 3))
        self.busy_window = int(cfg.get("busy_window_seconds", 8))
        self.count_neighbours = bool(cfg.get("count_neighbours", True))
        self._saved_default = None
        self._saved_v6 = []
        self._route_cmd = None
        self._hold_stop = None
        self._holder = None
        self._arping = shutil.which("arping") or shutil.which("/usr/sbin/arping")

    def prefix(self):
        if self.mode == "uid":
            return ["sudo", "-n", "-u", self.user]
        return []

    def address(self):
        return interface_address(self.iface)

    def up(self):
        return bool(self.address())

    # -- подъём канала (режим switch) -----------------------------------

    def activate(self):
        """
        Поднимает канал и ждёт адрес.

        ВАЖНО: раньше здесь проверялось только наличие IP-адреса на интерфейсе.
        Если `nmcli connection up` не срабатывал (не тот профиль, NetworkManager
        ещё занят переключением, нет /etc/sudoers.d правила на nmcli и т.п.),
        интерфейс мог по-прежнему стоять на ПРЕЖНЕЙ сети и держать её же адрес —
        цикл видел «адрес есть» и тут же запускал замер, который на самом деле
        шёл по старой (часто более быстрой) сети, а результат записывался под
        меткой новой. Отсюда и «2,4 ГГц» на 900 Мбит/с: по факту это была
        предыдущая сеть — 5 ГГц или Ethernet. Теперь дополнительно сверяем
        реально активный профиль nmcli с тем, что просили поднять.
        """
        if self.up_command:
            command = self.up_command.split()
        elif self.connection:
            command = ["sudo", "-n", "nmcli", "connection", "up",
                       "id", self.connection, "ifname", self.iface]
        else:
            command = ["sudo", "-n", "ip", "link", "set", self.iface, "up"]

        log(f"[{self.label}] поднимаю канал: {' '.join(command)}")
        _run_quiet(command, timeout=self.wait_seconds)

        deadline = time.time() + self.wait_seconds
        while time.time() < deadline:
            if self.address() and self._on_expected_connection() and self._nm_settled():
                time.sleep(self.settle_seconds)
                return None
            time.sleep(1.5)

        if self.connection and not self._on_expected_connection():
            return (
                f"{self.iface} не переключился на «{self.connection}» "
                f"(сейчас там: {self._active_connection() or 'нет сети'}) — "
                f"проверьте профиль nmcli и sudoers для nmcli, замер отменён, "
                f"чтобы не приписать чужую скорость этому каналу"
            )
        return f"{self.iface} не получил адрес за {self.wait_seconds} с"

    def _active_connection(self):
        """Имя nmcli-профиля, реально активного на интерфейсе прямо сейчас."""
        out = _run_quiet(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", self.iface])
        name = (out or "").strip().splitlines()[0].strip() if out and out.strip() else ""
        return name or None

    def _on_expected_connection(self):
        """Для switch-канала с именованным профилем — та ли это сеть, что нужна.
        Без этой проверки неудавшееся переключение остаётся незамеченным."""
        if not self.connection:
            return True
        return self._active_connection() == self.connection

    def _nm_settled(self):
        """
        NetworkManager закончил настройку интерфейса?

        `nmcli connection up` возвращает управление раньше, чем NM применит
        итоговую конфигурацию: DHCP-ответ может прийти через пару секунд и
        NM переустановит маршруты на устройстве, снеся наш маршрут с метрикой 0.
        Проверка маршрута при этом успевала пройти, а замер уже уходил через
        прежний интерфейс — утечка вида «100% байт через провод» на Wi-Fi.
        Состояние 100 означает, что настройка завершена и переписывать маршруты
        NM больше не будет.
        """
        if not self.connection:
            return True
        out = _run_quiet(["nmcli", "-g", "GENERAL.STATE", "device", "show", self.iface])
        return "100" in (out or "")

    def deactivate(self):
        if self.down_command:
            _run_quiet(self.down_command.split(), timeout=20)

    def _apply_default_route(self, extra_args=None):
        """
        Кладёт поверх свой маршрут по умолчанию (для switch — с метрикой 0,
        чтобы гарантированно перебить маршруты NetworkManager) и проверяет,
        что трафик действительно пошёл через нужный интерфейс. Если нет —
        откатывает и поднимает исключение, а не молча меряет чужой канал.
        Раньше эту проверку делал только режим switch; route полагался на
        то, что `ip route replace` не может не сработать, и в случае сбоя
        (например, нет sudoers-правила) замер тихо уходил через прежний
        маршрут под чужой меткой.
        """
        gateway = interface_gateway(self.iface)
        if not gateway:
            raise RuntimeError(
                f"не нашёл шлюз для {self.iface}. Проверьте: "
                f"nmcli -g IP4.GATEWAY device show {self.iface}"
            )
        cmd = ["sudo", "-n", "ip", "route", "replace", "default",
               "via", gateway, "dev", self.iface]
        if extra_args:
            cmd += extra_args
        self._route_cmd = cmd
        self._saved_default = (gateway, self.iface, tuple(extra_args or ()))

        # Одной установки мало: NetworkManager может переписать маршруты уже
        # после неё. Ставим и перепроверяем несколько раз с паузой — если
        # маршрут снесли, возвращаем его и убеждаемся, что он держится.
        check = ""
        for attempt in range(4):
            _run_quiet(cmd)
            time.sleep(1.2)
            check = _run_quiet(["ip", "route", "get", "77.88.55.88"]) or ""
            if f"dev {self.iface}" in check:
                if attempt:
                    log(f"[{self.label}] маршрут пришлось вернуть "
                        f"(попытка {attempt + 1}): NetworkManager его переписал")
                break
        else:
            self._restore_route()
            raise RuntimeError(
                f"маршрут не удержался на {self.iface}: {check.strip().splitlines()[:1]}"
            )
        time.sleep(0.6)

    def _prefer_route(self):
        self._apply_default_route(extra_args=["metric", "0"])

    def _announce_arp(self):
        """
        Объявляет роутеру: адрес этого интерфейса находится за ЕГО MAC-адресом.

        Маршруты у нас управляют только исходящими пакетами. Куда придёт ответ,
        решает ARP-таблица роутера, а там из-за прошлых утечек (или просто
        потому, что оба интерфейса в одной подсети) адрес одного интерфейса
        может числиться за MAC-адресом другого. Тогда запросы уходят по нужному
        каналу, а вся закачка возвращается по чужому — маршрут при этом
        выглядит совершенно правильным. Gratuitous ARP переписывает эту запись.
        """
        if not self._arping:
            return
        address = self.address()
        if not address:
            return
        _run_quiet(["sudo", "-n", self._arping, "-U", "-I", self.iface,
                    "-c", "2", address], timeout=8)

    def _start_route_holder(self):
        """
        Всё время замера удерживает наш маршрут по умолчанию.

        Разовой установки и проверки не хватает: NetworkManager синхронизирует
        маршруты устройства не только сразу после подключения, но и позже —
        уже посреди замера. Тогда наш маршрут с метрикой 0 исчезает, трафик
        уходит через провод, а разовая проверка этого не видит, потому что
        отработала раньше. Поэтому раз в две секунды переустанавливаем маршрут
        заново: команда идемпотентная и стоит доли миллисекунды.
        """
        if not self._route_cmd:
            return
        self._hold_stop = threading.Event()

        def hold():
            ticks = 0
            while not self._hold_stop.wait(2.0):
                _run_quiet(self._route_cmd)
                ticks += 1
                # Раз в 10 с напоминаем роутеру, за каким MAC-адресом наш IP:
                # он может переучиться посреди замера и снова завернуть ответы
                # на другой интерфейс.
                if ticks % 5 == 0:
                    self._announce_arp()

        self._holder = threading.Thread(target=hold, daemon=True,
                                        name=f"route-hold-{self.iface}")
        self._holder.start()

    def _stop_route_holder(self):
        if self._hold_stop:
            self._hold_stop.set()
        if self._holder:
            self._holder.join(timeout=3)
        self._hold_stop = None
        self._holder = None

    def _suppress_foreign_ipv6(self):
        """
        Убирает маршруты IPv6 по умолчанию, ведущие через ДРУГИЕ интерфейсы.

        `ip route` работает только с IPv4, поэтому весь IPv6 оставался вне
        нашего управления: маршрут IPv4 честно переводился на Wi-Fi, а клиент
        (Go предпочитает IPv6, когда тот доступен) уходил по IPv6 через провод.
        Внешне это выглядит как «маршрут правильный, а байты на другом интерфейсе».
        """
        self._saved_v6 = []
        out = _run_quiet(["ip", "-6", "route", "show", "default"]) or ""
        for line in out.strip().splitlines():
            line = line.strip()
            if not line or f"dev {self.iface}" in line:
                continue
            spec = line.replace(" pref medium", "").replace(" linkdown", "").strip()
            _run_quiet(["sudo", "-n", "ip", "-6", "route", "del", *spec.split()])
            self._saved_v6.append(spec)
        if self._saved_v6:
            log(f"[{self.label}] на время замера убрал маршрут IPv6 мимо "
                f"{self.iface}: {len(self._saved_v6)} шт.")

    def _restore_ipv6(self):
        for spec in getattr(self, "_saved_v6", []):
            _run_quiet(["sudo", "-n", "ip", "-6", "route", "add", *spec.split()])
        self._saved_v6 = []

    def _restore_route(self):
        if not self._saved_default:
            return
        gateway, iface, extra_args = self._saved_default
        _run_quiet(["sudo", "-n", "ip", "route", "del", "default",
                    "via", gateway, "dev", iface, *extra_args])
        self._saved_default = None

    def __enter__(self):
        if self.mode == "switch":
            self._prefer_route()
        if self.mode == "route":
            self._apply_default_route()
        if self.mode in ("route", "switch"):
            self._suppress_foreign_ipv6()
            self._announce_arp()
            self._start_route_holder()
        return self

    def __exit__(self, *_exc):
        if self.mode in ("route", "switch"):
            self._stop_route_holder()
            self._restore_ipv6()
            self._restore_route()
        return False

    def as_dict(self):
        return {"id": self.id, "iface": self.iface, "label": self.label,
                "color": self.color, "wireless": self.wireless,
                "drives_meter": self.drives_meter, "mode": self.mode,
                "connection": self.connection}


DEFAULT_PALETTE = ["#4CC9F0", "#FF9F4A", "#9B8CFF", "#4FC08D", "#E8B84B", "#F2604C"]


def build_links(cfg):
    """Из конфигурации делает список каналов. Пустой список — один канал по умолчанию."""
    raw = cfg.get("interfaces") or []
    if not raw:
        link = Link({"id": "default", "iface": "", "label": "Канал",
                     "mode": "none", "drives_meter": True, "wireless": False})
        link.color = DEFAULT_PALETTE[0]
        return [link]
    links = []
    for index, item in enumerate(raw):
        link = Link(item, index)
        link.color = link.color or DEFAULT_PALETTE[index % len(DEFAULT_PALETTE)]
        links.append(link)
    if not any(l.drives_meter for l in links):
        links[0].drives_meter = True
    source = (cfg.get("meter") or {}).get("source_interface")
    if source:
        for link in links:
            link.drives_meter = link.id == source or link.iface == source
    return links


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 1,
    download REAL,
    upload   REAL,
    ping     REAL,
    jitter   REAL,
    loss     REAL,
    voltage  REAL,
    zone     TEXT,
    server   TEXT,
    isp      TEXT,
    ipv4     TEXT,
    ipv6     TEXT,
    region   TEXT,
    asn      TEXT,
    duration REAL,
    error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(ts);
"""

EXTRA_COLUMNS = (
    ("ipv4", "TEXT"), ("ipv6", "TEXT"), ("region", "TEXT"), ("asn", "TEXT"),
    ("link", "TEXT"), ("iface", "TEXT"), ("ssid", "TEXT"), ("band", "TEXT"),
    ("freq", "INTEGER"), ("channel", "INTEGER"), ("signal", "REAL"),
    ("noise", "REAL"), ("tx_rate", "REAL"), ("rx_rate", "REAL"), ("busy", "REAL"),
    ("neighbours", "INTEGER"), ("neighbours_near", "INTEGER"),
)


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(measurements)")}
            for column, ctype in EXTRA_COLUMNS:
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE measurements ADD COLUMN {column} {ctype}")
            self._conn.commit()

    def add(self, row):
        row = dict(row)
        for column, _ in EXTRA_COLUMNS:
            row.setdefault(column, None)
        with self._lock:
            self._conn.execute(
                """INSERT INTO measurements
                   (ts, ok, download, upload, ping, jitter, loss, voltage, zone,
                    server, isp, ipv4, ipv6, region, asn, link, iface, ssid, band,
                    freq, channel, signal, noise, tx_rate, rx_rate, busy,
                    neighbours, neighbours_near, duration, error)
                   VALUES (:ts,:ok,:download,:upload,:ping,:jitter,:loss,:voltage,
                           :zone,:server,:isp,:ipv4,:ipv6,:region,:asn,:link,:iface,
                           :ssid,:band,:freq,:channel,:signal,:noise,:tx_rate,:rx_rate,
                           :busy,:neighbours,:neighbours_near,:duration,:error)""",
                row,
            )
            self._conn.commit()

    def latest(self, link=None, only_ok=True):
        clauses, params = [], []
        if only_ok:
            clauses.append("ok=1")
        if link:
            clauses.append("link=?")
            params.append(link)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM measurements{where} ORDER BY ts DESC LIMIT 1", params
            ).fetchone()
        return dict(row) if row else None

    def latest_by_link(self, links):
        return {link: self.latest(link=link) for link in links}

    def history(self, hours=24, limit=12000, link=None):
        since = int(time.time() - hours * 3600)
        sql = "SELECT * FROM measurements WHERE ts >= ?"
        params = [since]
        if link:
            sql += " AND link=?"
            params.append(link)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM measurements ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, hours=24):
        """Статистика по каждому каналу отдельно."""
        since = int(time.time() - hours * 3600)
        with self._lock:
            rows = self._conn.execute(
                """SELECT COALESCE(link,'default') AS link, COUNT(*) AS n,
                          AVG(download) AS avg_down, MAX(download) AS max_down,
                          MIN(download) AS min_down, AVG(upload) AS avg_up,
                          MAX(upload) AS max_up, AVG(ping) AS avg_ping,
                          AVG(jitter) AS avg_jitter, AVG(signal) AS avg_signal,
                          AVG(busy) AS avg_busy, AVG(neighbours) AS avg_neighbours
                   FROM measurements WHERE ts >= ? AND ok=1
                   GROUP BY COALESCE(link,'default')""",
                (since,),
            ).fetchall()
            fails = self._conn.execute(
                """SELECT COALESCE(link,'default') AS link, COUNT(*) AS n
                   FROM measurements WHERE ts >= ? AND ok=0
                   GROUP BY COALESCE(link,'default')""",
                (since,),
            ).fetchall()
        out = {r["link"]: dict(r) for r in rows}
        for row in fails:
            out.setdefault(row["link"], {"link": row["link"]})["failures"] = row["n"]
        for value in out.values():
            value.setdefault("failures", 0)
        return out


# --------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------

class Application:
    def __init__(self, cfg, use_gpio=True):
        self.cfg = cfg
        self.store = Store(os.path.join(BASE_DIR, cfg["database"]))
        self.meter = Meter(cfg, use_gpio=use_gpio)
        self.tester = Measurer(cfg)
        self.links = build_links(cfg)
        self.wifi = WirelessWatcher()

        self.status = "idle"
        self.status_note = ""
        self.next_ts = 0
        self.last_error = None
        self.started_at = time.time()

        names = ", ".join(f"{l.label} ({l.iface or 'по умолчанию'}, {l.mode})" for l in self.links)
        log(f"Каналы: {names}")
        check_arp_flux(self.links)

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Lock()

    @property
    def meter_link(self):
        for link in self.links:
            if link.drives_meter:
                return link
        return self.links[0]

    # -- цикл -----------------------------------------------------------

    def start(self):
        if self.cfg["meter"].get("sweep_on_start", True):
            threading.Thread(target=self.meter.sweep, daemon=True).start()
        last = self.store.latest(link=self.meter_link.id)
        if last and last.get("download") is not None:
            self.meter.set_speed(last["download"])
            self.meter.set_zone(self.meter.zone_for(last["download"]))
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(3)
        while not self._stop.is_set():
            self.measure()
            interval = max(30, int(self.cfg["interval_minutes"] * 60))
            self.next_ts = int(time.time() + interval)
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def trigger_now(self):
        if self._busy.locked():
            return False
        self._wake.set()
        return True

    # -- один канал -----------------------------------------------------

    def measure_link(self, link):
        """
        Меряет один канал.

        Порядок важен: сначала канал поднимается, потом снимается занятость эфира
        в тишине, и только затем запускается замер скорости. Если снимать занятость
        во время замера, мы намеряем собственный трафик, а не соседей.
        """
        now = int(time.time())

        if link.mode == "switch":
            error = link.activate()
            if error:
                self._store_failure(link, now, error, {})
                log(f"[{link.label}] пропуск: {error}")
                return None
        elif link.iface and link.mode != "none" and not link.up():
            error = f"интерфейс {link.iface} без адреса"
            self._store_failure(link, now, error, {})
            log(f"[{link.label}] пропуск: {error}")
            return None

        wifi = {}
        if link.wireless and link.iface:
            wifi = self.wifi.sample(link.iface)
            if link.busy_window > 0:
                self.status_note = f"{link.label}: слушаю эфир"
                wifi.update(self.wifi.busy_window(link.iface, link.busy_window))

        self.status_note = f"Замер: {link.label}"
        try:
            with link:
                counters_before = _iface_counters()
                result = self.tester.run(link=link)
                carrier, share = _traffic_carrier(counters_before, _iface_counters())
                # Соседей считаем здесь, когда замер уже позади
                if wifi and wifi.get("busy") is None and link.count_neighbours:
                    wifi.update(self.wifi.neighbours(link.iface, wifi.get("freq")))
            # Прямая проверка: трафик замера обязан был пройти через интерфейс
            # этого канала. Если основную часть байт принял другой интерфейс,
            # цифра относится к чужому каналу — такую в базу писать нельзя.
            if link.iface and carrier and carrier != link.iface and share >= 0.5:
                raise RuntimeError(
                    f"трафик замера прошёл через {carrier} ({share * 100:.0f}% байт), "
                    f"а не через {link.iface} — результат отброшен, чтобы не приписать "
                    f"каналу «{link.label}» чужую скорость"
                )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:400]
            self.last_error = f"{link.label}: {error}"
            log(f"[{link.label}] ошибка замера: {error}")
            self._store_failure(link, now, error, wifi)
            if link.drives_meter:
                self.meter.set_voltage(0.0)
                self.meter.set_zone("red")
            return None

        source = self.cfg["meter"].get("source", "download")
        value = result["download"] if source != "upload" else (result["upload"] or 0)
        if source == "max":
            value = max(result["download"], result["upload"] or 0)
        volts = self.meter.speed_to_voltage(value)
        zone = self.meter.zone_for(value)

        if link.drives_meter:
            self.meter.set_speed(value)
            self.meter.set_zone(zone)
            self.last_error = None

        row = {
            "ts": now, "ok": 1,
            "download": result["download"], "upload": result["upload"],
            "ping": result.get("ping"), "jitter": result.get("jitter"),
            "loss": result.get("loss"), "voltage": round(volts, 3), "zone": zone,
            "server": result.get("server"), "isp": result.get("isp"),
            "ipv4": result.get("ipv4"), "ipv6": result.get("ipv6"),
            "region": result.get("region"), "asn": result.get("asn"),
            "link": link.id, "iface": link.iface,
            "duration": result.get("duration"), "error": None,
            **wifi,
        }
        self.store.add(row)

        extra = ""
        if wifi.get("signal") is not None:
            extra = f", сигнал {wifi['signal']:.0f} дБм"
            if wifi.get("channel"):
                extra += f", канал {wifi['channel']}"
            if wifi.get("rx_rate"):
                extra += f", линк {wifi['rx_rate']:.0f}↓"
                if wifi.get("tx_rate"):
                    extra += f"/{wifi['tx_rate']:.0f}↑ Мбит/с"
            if wifi.get("busy") is not None:
                extra += f", эфир занят {wifi['busy']:.0f}%"
            elif wifi.get("neighbours") is not None:
                extra += f", чужих сетей рядом {wifi['neighbours']}"
        log(f"[{link.label}] ↓{result['download']} ↑{result['upload']} Мбит/с, "
            f"пинг {result.get('ping')} мс{extra}"
            + (f" → {volts:.2f} В" if link.drives_meter else ""))

        # Замер не может быть быстрее физической скорости радиолинка. Если он
        # быстрее — значит, трафик ушёл не через этот интерфейс, и цифра чужая.
        self._warn_if_above_link_rate(link, result, wifi)
        return row

    @staticmethod
    def _warn_if_above_link_rate(link, result, wifi):
        rate = wifi.get("rx_rate")
        if not rate or result.get("download") is None:
            return
        # Запас 15 %: PHY-скорость плавает между опросом iw и самим замером.
        if result["download"] > rate * 1.15:
            log(f"[{link.label}] ВНИМАНИЕ: намерено {result['download']:.0f} Мбит/с "
                f"при физической скорости линка {rate:.0f} Мбит/с. Так не бывает — "
                f"скорее всего замер ушёл через другой интерфейс. "
                f"Проверьте: python3 internetometer.py --probe --iface {link.id}")

    def _store_failure(self, link, ts, error, wifi):
        self.store.add({
            "ts": ts, "ok": 0, "download": None, "upload": None, "ping": None,
            "jitter": None, "loss": None, "voltage": None, "zone": None,
            "server": None, "isp": None, "link": link.id, "iface": link.iface,
            "duration": None, "error": error, **wifi,
        })

    def measure(self):
        """Проходит по всем каналам по очереди: параллельно мерить нельзя, помешают друг другу."""
        if not self._busy.acquire(blocking=False):
            return None
        rows = []
        try:
            self.status = "testing"
            self.meter.activity(True)
            for link in self.links:
                self.status_note = f"Замер: {link.label}"
                row = self.measure_link(link)
                if row:
                    rows.append(row)
            return rows[0] if rows else None
        finally:
            self.meter.activity(False)
            self.status = "idle"
            self.status_note = ""
            self._busy.release()

    # -- данные для интерфейса ------------------------------------------

    def state(self):
        meter = self.meter.snapshot()
        per_link = {}
        for link in self.links:
            per_link[link.id] = self.store.latest(link=link.id)
        primary = per_link.get(self.meter_link.id) or next(
            (v for v in per_link.values() if v), None)
        zone = self.meter.zone_for(primary["download"]) if primary else "off"
        return {
            "now": int(time.time()),
            "status": self.status,
            "note": self.status_note,
            "next_ts": self.next_ts,
            "uptime": int(time.time() - self.started_at),
            "latest": primary,
            "links": [dict(link.as_dict(), latest=per_link.get(link.id)) for link in self.links],
            "meter_link": self.meter_link.id,
            "meter": meter,
            "zone": zone,
            "error": self.last_error,
            "engine": "Яндекс Интернетометр",
            "binary": self.tester.binary,
            "config": {
                "interval_minutes": self.cfg["interval_minutes"],
                "title": self.cfg.get("title"),
                "subtitle": self.cfg.get("subtitle"),
                "green_min": self.cfg["leds"]["green_min_mbps"],
                "yellow_min": self.cfg["leds"]["yellow_min_mbps"],
                "version": APP_VERSION,
            },
        }

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        self.meter.shutdown()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app: Application = None  # проставляется при создании сервера
    server_version = "Internetometer/" + APP_VERSION

    def log_message(self, *_args):
        pass

    # -- утилиты --------------------------------------------------------

    def _send_json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, mime):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- маршруты -------------------------------------------------------

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        app = self.app

        if url.path in ("/", "/index.html"):
            return self._send_file(INDEX_PATH, "text/html; charset=utf-8")

        if url.path == "/api/state":
            return self._send_json(app.state())

        if url.path == "/api/history":
            hours = float(query.get("hours", ["24"])[0])
            rows = app.store.history(hours=hours)
            grouped = {}
            for row in rows:
                grouped.setdefault(row.get("link") or "default", []).append(row)
            return self._send_json({
                "hours": hours,
                "series": grouped,
                "points": grouped.get(app.meter_link.id, next(iter(grouped.values()), [])),
                "stats": app.store.stats(hours=hours),
                "links": [link.as_dict() for link in app.links],
            })

        if url.path == "/api/links":
            return self._send_json({"links": [link.as_dict() for link in app.links]})

        if url.path == "/api/journal":
            limit = int(query.get("limit", ["15"])[0])
            return self._send_json({"rows": app.store.recent(limit=limit)})

        if url.path == "/api/health":
            return self._send_json({"ok": True, "version": APP_VERSION})

        self.send_error(404, "not found")

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        app = self.app
        payload = self._body()

        if url.path == "/api/test":
            started = app.trigger_now()
            return self._send_json({"started": started, "busy": not started})

        if url.path == "/api/sweep":
            threading.Thread(target=app.meter.sweep, daemon=True).start()
            return self._send_json({"ok": True})

        if url.path == "/api/meter":
            if payload.get("mode") == "auto":
                app.meter.release_manual()
                latest = app.store.latest()
                if latest and latest.get("download") is not None:
                    app.meter.set_speed(latest["download"])
                return self._send_json({"ok": True, "manual": False})
            volts = float(payload.get("voltage", 0))
            app.meter.set_voltage(volts, manual=True)
            return self._send_json({"ok": True, "manual": True, "voltage": volts})

        self.send_error(404, "not found")


def serve(app):
    Handler.app = app
    server = ThreadingHTTPServer((app.cfg["http_host"], int(app.cfg["http_port"])), Handler)
    server.daemon_threads = True
    log(f"Веб-интерфейс: http://{app.cfg['http_host']}:{app.cfg['http_port']}/")
    return server


def check_arp_flux(links):
    """
    Предупреждает, если несколько интерфейсов сидят в одной подсети, а защита
    от ARP flux не включена. Без неё роутер шлёт ответный трафик не на тот
    интерфейс, и замер меряет чужой канал при формально верном маршруте.
    """
    subnets = {}
    for link in links:
        if not link.iface:
            continue
        out = _run_quiet(["ip", "-4", "-o", "route", "show", "dev", link.iface, "scope", "link"])
        for line in (out or "").splitlines():
            net = line.split()[0] if line.split() else ""
            if net:
                subnets.setdefault(net, set()).add(link.iface)
    shared = {net: ifaces for net, ifaces in subnets.items() if len(ifaces) > 1}
    if not shared:
        return
    try:
        with open("/proc/sys/net/ipv4/conf/all/arp_ignore", encoding="ascii") as handle:
            arp_ignore = handle.read().strip()
    except OSError:
        return
    if arp_ignore == "0":
        for net, ifaces in shared.items():
            log(f"ВНИМАНИЕ: {' и '.join(sorted(ifaces))} в одной подсети {net}, "
                f"а защита от ARP flux выключена (arp_ignore=0). Ответный трафик "
                f"может приходить не на тот интерфейс, и замеры будут неверными. "
                f"Выполните: python3 internetometer.py --setup-routing > setup.sh "
                f"&& sudo bash setup.sh")


def routing_script(cfg):
    """
    Печатает скрипт, который разводит трафик замеров по интерфейсам.

    Идея: для каждого канала заводится отдельный системный пользователь и своя
    таблица маршрутизации. Правило «ip rule uidrange» отправляет весь трафик
    этого пользователя в его таблицу. Клиент Яндекса запускается из-под нужного
    пользователя и физически выходит через нужный интерфейс, при этом остальная
    сеть Raspberry Pi не меняется.
    """
    all_links = [l for l in build_links(cfg) if l.iface]
    links = [l for l in all_links if l.mode == "uid"]
    switching = [l for l in all_links if l.mode in ("switch", "route")]
    if not links and not switching:
        return "#!/usr/bin/env bash\n# В config.json нет каналов, требующих настройки."

    out = [
        "#!/usr/bin/env bash",
        "# Разводка трафика замеров по интерфейсам. Запускать через sudo.",
        "# Скрипт идемпотентный: можно выполнять повторно и вешать на автозапуск.",
        "set -euo pipefail",
        "",
    ]
    for link in links:
        out += [
            f'# ---------- {link.label} ({link.iface}) ----------',
            f'IFACE="{link.iface}"; USER_NAME="{link.user}"; TABLE={link.table}',
            'if ! id "$USER_NAME" >/dev/null 2>&1; then',
            '  useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"',
            'fi',
            'UID_N="$(id -u "$USER_NAME")"',
            'ADDR="$(ip -4 -o addr show dev "$IFACE" | awk \'{print $4}\' | head -1)"',
            'GW="$(ip -4 route show default dev "$IFACE" | awk \'/via/ {print $3}\' | head -1)"',
            'if [ -z "$ADDR" ]; then echo "  $IFACE без адреса, пропускаю"; else',
            '  SUBNET="$(ipcalc -n "$ADDR" 2>/dev/null | awk -F= \'/^NETWORK/{print $2}\')" || true',
            '  [ -n "${SUBNET:-}" ] || SUBNET="$(ip -4 route show dev "$IFACE" scope link | awk \'{print $1}\' | head -1)"',
            '  ip route flush table "$TABLE" 2>/dev/null || true',
            '  ip route add "$SUBNET" dev "$IFACE" scope link table "$TABLE"',
            '  [ -n "$GW" ] && ip route add default via "$GW" dev "$IFACE" table "$TABLE"',
            '  ip rule del uidrange "$UID_N-$UID_N" 2>/dev/null || true',
            '  ip rule add uidrange "$UID_N-$UID_N" lookup "$TABLE" priority $((1000 + TABLE))',
            '  echo "  $IFACE → таблица $TABLE, пользователь $USER_NAME (uid $UID_N)"',
            'fi',
            '',
        ]

    binary = (cfg.get("yandex") or {}).get("binary", "internetometer")
    service_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "pi"

    sudo_lines = []
    if links:
        sudo_lines.append(
            f'{service_user} ALL=(' + ",".join(l.user for l in links)
            + f') NOPASSWD: /usr/local/bin/{binary}')
    if switching:
        # Режимы switch и route меняют маршрут по умолчанию и переподключают Wi-Fi
        sudo_lines.append(f'{service_user} ALL=(root) NOPASSWD: /usr/sbin/ip, /sbin/ip, /usr/bin/nmcli, /usr/sbin/arping, /usr/bin/arping')

    out += [
        "# ---------- разделение интерфейсов в одной подсети ----------",
        "# Когда провод и Wi-Fi воткнуты в ОДИН роутер, они оказываются в одной",
        "# подсети. По умолчанию Linux отвечает на ARP-запрос про любой свой",
        "# адрес с любого интерфейса (ARP flux), поэтому роутер запоминает адрес",
        "# Wi-Fi за MAC-адресом провода и шлёт ответный трафик в провод. Запросы",
        "# уходят по нужному интерфейсу, а вся закачка приходит по чужому —",
        "# замер получается не тот, хотя маршрут выставлен верно.",
        "#   arp_ignore=1   отвечать только про адреса того интерфейса, куда пришёл запрос",
        "#   arp_announce=2 представляться адресом того интерфейса, откуда уходит пакет",
        "#   rp_filter=2    мягкая проверка обратного пути, иначе ядро отбросит ответы",
        "# Утилита arping рассылает gratuitous ARP: без неё роутер продолжит",
        "# слать ответный трафик на тот интерфейс, который запомнил раньше.",
        "command -v arping >/dev/null || apt-get install -y iputils-arping",
        "cat > /etc/sysctl.d/99-internetometer.conf <<EOF",
        "net.ipv4.conf.all.arp_ignore = 1",
        "net.ipv4.conf.all.arp_announce = 2",
        "net.ipv4.conf.all.rp_filter = 2",
        "net.ipv4.conf.default.rp_filter = 2",
        "EOF",
        "sysctl -q --system",
        "ip neigh flush all || true",
        "",
        "# ---------- права без пароля для демона ----------",
        'cat > /etc/sudoers.d/internetometer <<EOF',
    ] + sudo_lines + [
        'EOF',
        'chmod 0440 /etc/sudoers.d/internetometer',
        'visudo -c -f /etc/sudoers.d/internetometer',
        "",
        'echo "Готово. Проверка:"',
    ]
    for link in links:
        out.append(f'echo "  sudo -u {link.user} curl -s https://api.ipify.org; echo"')
    for link in switching:
        if link.connection:
            out.append(f'echo "  sudo nmcli connection up id \'{link.connection}\' ifname {link.iface}"')
    return "\n".join(out)


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Интернетометр")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--no-gpio", action="store_true", help="эмуляция без железа")
    parser.add_argument("--test-once", action="store_true", help="один замер и выход")
    parser.add_argument("--sweep", action="store_true", help="прогнать стрелку и выйти")
    parser.add_argument("--set-volts", type=float, help="подать напряжение и ждать")
    parser.add_argument("--probe", action="store_true",
                        help="показать сырой ответ клиента и разбор полей")
    parser.add_argument("--iface", help="какой канал проверять в режиме --probe")
    parser.add_argument("--setup-routing", action="store_true",
                        help="напечатать скрипт настройки маршрутов по интерфейсам")
    parser.add_argument("--port", type=int, help="порт веб-сервера")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.port:
        cfg["http_port"] = args.port

    use_gpio = not args.no_gpio

    if args.setup_routing:
        print(routing_script(cfg))
        return

    if args.probe:
        measurer = Measurer(cfg)
        links = build_links(cfg)
        chosen = next((l for l in links if args.iface in (l.id, l.iface)), links[0]) \
            if args.iface else links[0]

        if chosen.iface:
            print(f"Канал: {chosen.label} ({chosen.iface}, привязка {chosen.mode})")

        # Режим switch обязан сначала поднять свой канал, иначе замер уйдёт
        # через тот интерфейс, который сейчас держит маршрут по умолчанию.
        if chosen.mode == "switch":
            print(f"Поднимаю подключение «{chosen.connection or chosen.iface}»…")
            failure = chosen.activate()
            if failure:
                print("Не удалось поднять канал:", failure)
                return

        if chosen.iface and chosen.wireless:
            info = WirelessWatcher().sample(chosen.iface)
            if info:
                print("Wi-Fi:", json.dumps(info, ensure_ascii=False))
                if info.get("busy_active") is None:
                    print("  счётчики занятости эфира драйвер не отдаёт "
                          "(iw survey dump пуст) — поле busy останется пустым")

        print("Команда:", " ".join(measurer.command(chosen)) or "— не найдена")

        # Заходим в контекст канала: он подменит маршрут по умолчанию на время замера
        with chosen:
            route = _run_quiet(["ip", "route", "get", "77.88.55.88"]).strip()
            if route:
                print("Трафик пойдёт через:", route.splitlines()[0])
            raw = measurer.raw(link=chosen)
        print("\n--- сырой вывод ---")
        print(raw.strip()[:4000])
        data = measurer.extract_json(raw)
        print("\n--- найденный JSON ---")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        result = measurer.parse(data)
        print("\n--- как я это понял ---")
        for field, source in measurer.mapping.items():
            print(f"  {field:9} ← {source}")
        print("\n--- итог ---")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.sweep:
        meter = Meter(cfg, use_gpio=use_gpio)
        meter.sweep()
        time.sleep(1)
        meter.shutdown()
        return

    if args.set_volts is not None:
        meter = Meter(cfg, use_gpio=use_gpio)
        meter.set_voltage(args.set_volts)
        divisions = meter.voltage_to_divisions(args.set_volts)
        print(f"Подано {args.set_volts:.3f} В — стрелка должна встать на {divisions:.1f} делений.")
        print("Ctrl+C для выхода.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        meter.shutdown()
        return

    app = Application(cfg, use_gpio=use_gpio)

    if args.test_once:
        row = app.measure()
        print(json.dumps(row, ensure_ascii=False, indent=2))
        time.sleep(3)
        app.shutdown()
        return

    app.start()
    server = serve(app)

    def bye(*_a):
        log("Останавливаюсь, свожу стрелку на ноль")
        threading.Thread(target=server.shutdown, daemon=True).start()
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        bye()


if __name__ == "__main__":
    main()
