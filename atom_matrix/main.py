"""
Atom Matrix — StamPLC remote indicator + control
================================================

M5Stack Atom Matrix (ESP32, 5x5 RGB matrix, single button) that:

  * Mirrors the StamPLC's compressor state via ESP-NOW (primary channel)
  * Falls back to HTTP if ESP-NOW drops
  * Sends ON / OFF commands over BOTH channels when the button is pressed
  * Only changes its display when the StamPLC actually confirms

Boot visuals:
  * BLUE snake  = board booted
  * YELLOW snake = WiFi connected
  * RED X       = WiFi failed
  * WHITE snake = waiting for StamPLC response after button press
  * All GREEN   = compressor ON
  * Screen OFF  = compressor OFF (power saving)
"""

import os, sys, io
import M5
from M5 import *
from m5espnow import M5ESPNow
import time
import network
import socket
from hardware import RGB


# ----- CONFIG -----
WIFI_SSID    = 'YourWiFiSSID'
WIFI_PASS    = 'YourWiFiPassword'
STAMPLC_IP   = '192.168.1.100'     # <- StamPLC IP (see its serial log)
STAMPLC_MAC  = '0123456789AB'      # <- StamPLC MAC (ESP-NOW peer)


espnow_0 = None
rgb = None
wlan_sta = None

espnow_mac = None
espnow_data = None
relay_status = False
wifi_ok = False
last_status_poll = 0
last_esp_now_rx = 0


# ----- helpers -----
def log(*args):
    print('[%6d ms] ' % time.ticks_ms(), *args)


# Spiral path through the 5x5 matrix (outer ring -> inner -> center)
SPIRAL = [
    0, 1, 2, 3, 4,
    9, 14, 19, 24,
    23, 22, 21, 20,
    15, 10, 5,
    6, 7, 8,
    13, 18,
    17, 16,
    11,
    12,
]


def snake_spiral(body_colors, step_ms=50, brightness=10):
    """Generic snake spiral: body_colors[0] is the head, the rest is the fading tail."""
    rgb.set_brightness(brightness)
    for head_idx in range(len(SPIRAL) + len(body_colors)):
        screen = [0] * 25
        for offset, color in enumerate(body_colors):
            pos = head_idx - offset
            if 0 <= pos < len(SPIRAL):
                screen[SPIRAL[pos]] = color
        rgb.set_screen(screen)
        time.sleep_ms(step_ms)
    rgb.set_screen([0] * 25)


def snake_blue():
    """Blue snake — board has started."""
    body = [0xffffff, 0x0088ff, 0x0044aa, 0x002266, 0x001133]
    snake_spiral(body, step_ms=50)
    log('boot snake (blue) done')


def snake_yellow():
    """Yellow snake — WiFi connected."""
    body = [0xffffff, 0xffcc00, 0xaa8800, 0x664400, 0x332200]
    snake_spiral(body, step_ms=50)
    log('wifi snake (yellow) done')


def snake_white_fast():
    """Fast white snake — shown while waiting for StamPLC response."""
    body = [0xffffff, 0xaaaaaa, 0x555555, 0x222222]
    snake_spiral(body, step_ms=22)


def show_wifi_err():
    """Red 'X' — WiFi failed."""
    rgb.set_brightness(10)
    x = [
        0xff0000, 0,       0,       0,       0xff0000,
        0,       0xff0000, 0,       0xff0000, 0,
        0,       0,       0xff0000, 0,       0,
        0,       0xff0000, 0,       0xff0000, 0,
        0xff0000, 0,       0,       0,       0xff0000,
    ]
    rgb.set_screen(x)


def show_on():
    """All green — compressor ON."""
    rgb.set_brightness(10)
    rgb.set_screen([0x00ff00] * 25)


def show_off():
    """Screen fully off — compressor OFF (power saving)."""
    rgb.set_screen([0] * 25)


def apply_state(on):
    """Change local state + display. Only called when the StamPLC confirms a state."""
    global relay_status
    relay_status = on
    if on:
        show_on()
    else:
        show_off()


# ----- HTTP helper (raw socket) -----
def http_get(path, timeout=3):
    if not wifi_ok:
        return None
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((STAMPLC_IP, 80))
        s.send(('GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n'
                % (path, STAMPLC_IP)).encode())
        resp = b''
        while True:
            chunk = s.recv(256)
            if not chunk:
                break
            resp += chunk
        if b'\r\n\r\n' in resp:
            return resp.split(b'\r\n\r\n', 1)[1]
        return resp
    except Exception as e:
        log('HTTP error:', e)
        return None
    finally:
        if s:
            try: s.close()
            except: pass


# ----- ESP-NOW callback -----
def espnow_recv_callback(espnow_obj):
    global espnow_mac, espnow_data, last_esp_now_rx
    espnow_mac, espnow_data = espnow_obj.recv_data()
    cmd = espnow_0._bytes_to(espnow_data, 0)
    last_esp_now_rx = time.ticks_ms()
    log('ESP-NOW <- cmd =', cmd)
    if cmd == 1:
        apply_state(True)
    elif cmd == 0:
        apply_state(False)


# ----- button: request change, show waiting snake, apply on response -----
def btnA_wasClicked_event(state):
    log('button A clicked')
    target_on = not relay_status

    # 1. fire ESP-NOW first (fast)
    espnow_0.send_data(1, 1 if target_on else 0)
    log('  -> ESP-NOW sent cmd =', 1 if target_on else 0)

    # 2. show fast white snake while the StamPLC processes
    snake_white_fast()

    # 3. HTTP reply reports the ACTUAL resulting state
    resp = http_get('/on' if target_on else '/off', timeout=3)
    if resp:
        s = resp.strip()
        log('  -> HTTP OK, reply =', s)
        apply_state(s == b'ON')
    else:
        log('  -> HTTP FAILED (waiting for ESP-NOW confirmation / poll)')


# ----- WiFi -----
def connect_wifi():
    global wlan_sta, wifi_ok
    log('WiFi: resetting radio...')
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_sta.active(False)
    time.sleep_ms(500)
    wlan_sta.active(True)
    log('WiFi: connecting to', WIFI_SSID)
    wlan_sta.connect(WIFI_SSID, WIFI_PASS)
    for _ in range(50):
        if wlan_sta.isconnected():
            break
        time.sleep_ms(200)
    if wlan_sta.isconnected():
        wifi_ok = True
        log('WiFi: CONNECTED, IP =', wlan_sta.ifconfig()[0])
    else:
        wifi_ok = False
        log('WiFi: FAILED (HTTP backup disabled)')


# ----- main lifecycle -----
def setup():
    global espnow_0, rgb, relay_status

    log('=== Atom Matrix boot ===')

    M5.begin()
    Widgets.fillScreen(0x000000)
    BtnA.setCallback(type=BtnA.CB_TYPE.WAS_CLICKED, cb=btnA_wasClicked_event)

    relay_status = False

    rgb = RGB()
    rgb.set_screen([0] * 25)
    rgb.set_brightness(10)
    log('RGB matrix initialized')

    # BLUE snake = board started
    snake_blue()

    # WiFi -> YELLOW snake on success, red X on failure
    connect_wifi()
    if wifi_ok:
        snake_yellow()
    else:
        show_wifi_err()
        time.sleep_ms(800)

    # screen off (relay starts OFF)
    rgb.set_screen([0] * 25)

    # ESP-NOW (primary channel)
    espnow_0 = M5ESPNow(0)
    espnow_0.set_irq_callback(espnow_recv_callback)
    espnow_0.set_add_peer(STAMPLC_MAC, 1, 0, False)
    log('ESP-NOW: peer registered, listening')

    # Ask the StamPLC for its current state
    espnow_0.send_data(1, 2)
    log('ESP-NOW: sent status query (cmd=2)')

    if wifi_ok:
        resp = http_get('/status', timeout=2)
        if resp:
            s = resp.strip()
            log('HTTP /status =', s)
            apply_state(s == b'ON')


def loop():
    global last_status_poll
    M5.update()
    now = time.ticks_ms()

    # No animation in OFF state - screen stays off for power economy.
    # HTTP /status backup poll every 3 s.
    if wifi_ok and time.ticks_diff(now, last_status_poll) > 3000:
        last_status_poll = now
        resp = http_get('/status', timeout=2)
        if resp:
            s = resp.strip()
            if s == b'ON' and not relay_status:
                log('HTTP poll: corrected to ON')
                apply_state(True)
            elif s == b'OFF' and relay_status:
                log('HTTP poll: corrected to OFF')
                apply_state(False)


if __name__ == '__main__':
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            from utility import print_error_msg
            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
