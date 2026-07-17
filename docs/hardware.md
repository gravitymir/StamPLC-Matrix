# Hardware setup

Wiring, GPIO map, and bill of materials for the StamPLC + Atom Matrix build.

---

## Bill of materials

| Qty | Part | Role | Notes |
|---:|---|---|---|
| 1 | **M5Stack StamPLC** (ESP32-S3, STAMP S3A) | Main controller | 24 VDC input, 4× internal relays, 8× isolated inputs, LCD, 3 buttons, RTC |
| 1 | **M5Stack Atom Matrix** (ESP32) | Remote indicator + toggle button | 5×5 WS2812 RGB matrix, 1× click button |
| 2 | **Fotek SSR-40 DA-H** | Solid-state relay for 220 V AC compressor | 3–32 VDC input, 90–480 VAC output, 40 A max — one is main, second is optional redundancy or extra load |
| 1 | **Hobby servo** (SG90 / MG996R class) | Mechanical status indicator on the OFF/ON lever | 5 V DC, ~500 mA under load |
| 1 | **5 V DC power supply** (≥1 A) | Servo power (external, not from StamPLC 5 V rail) | Prevents brownouts under servo stall |
| 1 | **DIN rail** + terminal blocks | Mounting the StamPLC + SSRs | Optional but professional |
| 1 | **10 kΩ resistor** | Pull-up on GPIO 41 to keep SSR OFF during boot | Recommended for safety |

---

## GPIO pin map — StamPLC (ESP32-S3)

The StamPLC exposes two Grove ports and a 2×8 GPIO.EXT header.

| Pin | Where | Function in this project |
|---|---|---|
| **G40** | GPIO.EXT top-left (green) | Servo PWM signal — 50 Hz LEDC |
| **G41** | GPIO.EXT bottom-left (green) | SSR control (LOW = ON, inverted logic via 5 V source) |
| **G5**  | PORT.C blue Grove | Free (unused in current build) |
| **G4**  | PORT.C blue Grove | Free |
| **G1/G2** | PORT.A red Grove (I²C) | Free (no I²C peripherals used) |

### Why inverted logic on G41?

The Fotek SSR-40 DA-H input is powered from the StamPLC's **5 V OUT** on one
side. The other side goes to **G41**. When G41 is LOW, current flows through
the SSR's internal opto-LED (5 V - 0 V = 5 V, above trigger threshold) →
SSR conducts. When G41 is HIGH (3.3 V), only 1.7 V is across the input → below
trigger → SSR off.

This lets a 3.3 V logic pin drive a 5 V+ SSR reliably by sinking rather than
sourcing current.

---

## Servo mechanism

The servo horn is attached to the compressor's OFF/ON lever via a flat metal
arm. Two angles matter:

| Angle | Meaning | Duty (10-bit) | Pulse width |
|---|---|---|---|
| `0°` | lever pushed to OFF label | 31 | ~0.6 ms |
| `60°` | lever pulled to ON label | 62 | ~1.2 ms |

The exact angles depend on the mechanical geometry of your lever — tune with
`GET /servo/<angle>` from the web UI or curl.

After each move the PWM signal is cut (`duty(0)`), so the servo coasts and
draws no holding current. Physical friction holds the lever in position.

---

## Wiring diagram (text)

```
                                  220 V AC mains
                                       │
                                     [ 10 A fuse ]
                                       │
                              L ───────┴─── SSR A1
                                             │
                              SSR A2 ─────── L → compressor
                              N ─────────────── N → compressor
                              PE ────────────── PE → compressor


                       StamPLC GPIO.EXT
                       ┌───────────────┐
   Servo signal ←──────┤ G40           │
                       │               │
                       │        5V OUT ├─── SSR input +  (via 10 kΩ pull-up
                       │           G41 ├──┬─ SSR input − ─┘ to 5V for safe boot)
                       │           GND ├──┴─ Servo GND
                       └───────────────┘

     Servo V+ ←── external 5 V (NOT from StamPLC 5 V OUT)
     Servo GND ←─ common ground shared with StamPLC GND and external 5 V supply
```

Key points:

- **Both AC wires** must be in the loop — SSR breaks only the live wire, neutral
  goes directly to the load.
- **Fuse the live wire** — 10 A slow-blow between mains and SSR A1 protects
  the SSR and downstream wiring if the compressor shorts.
- **SSR heatsink is mandatory** above ~5 A continuous. Don't skimp.
- **10 kΩ pull-up on G41 → 5 V** guarantees the SSR is OFF during the ESP32-S3
  boot window when GPIO 41 is floating (before firmware initializes it).
- **Common ground** for external 5 V servo supply and StamPLC — otherwise the
  servo signal can float and cause jitter.

---

## Atom Matrix wiring

Atom Matrix is powered by USB-C (5 V) and needs no other wiring — it's the
remote pull for the compressor plus an in-your-face status LED.

Place it where you can see it and reach it. Typical spot: on a metal cabinet
near the workshop door.

---

## Power topology

```
    Wall socket              StamPLC internal            External load
    ────────────      ┌─────────────────────┐         ────────────────
    12–24 VDC → ──────┤ VIN                  │
                      │                      │
                      │         5V OUT ──────┼───→ SSR input
                      │                      │       └──← G41 (sink)
                      │         GND    ──────┼───→ SSR input GND
                      │                      │
                      │         G40   ──────┼───→ Servo signal
                      └─────────────────────┘

    Separate 5V PSU ─────────→ Servo V+  (avoids brownouts)
                    └─────────→ common GND
```

---

## Testing checklist

Before putting 220 V on the SSR outputs, verify the DC side:

1. Boot the StamPLC — LCD should show grey **OFF**.
2. `curl http://<ip>/on` — the small **LED on the SSR front** should light red
   (input triggered). The servo should physically move to the ON angle. Chime
   plays.
3. `curl http://<ip>/off` — SSR LED goes dark, servo returns to OFF angle.
4. Only after both steps are consistent, connect mains through the SSR.

Test with a **60 W incandescent bulb** in place of the compressor first — it
should light bright (ON) or completely dark (OFF), no dim intermediate state.
If you measure phantom AC voltage on the SSR output with no load — that's
normal RC-snubber leakage, not a problem. It disappears the moment the load
is connected.
