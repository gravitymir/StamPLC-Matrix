# StamPLC HTTP API

Full reference for the HTTP interface exposed by the StamPLC. Aimed at external
servers, scripts, cron jobs, home-automation systems, or any integration that
wants to control the compressor programmatically.

---

## Base

| Parameter | Value |
|---|---|
| **IP address** | e.g. `192.168.3.75` — printed in the StamPLC's serial log at boot |
| **Port** | `80` |
| **Protocol** | HTTP/1.0 or HTTP/1.1 |
| **Method** | All endpoints are `GET` |
| **Auth** | None — assumed to be on a trusted LAN |
| **Content-Type of responses** | `text/plain` for state, `application/json` for structured data |

---

## Relay control endpoints

### `GET /on` — turn compressor ON
Idempotent: does nothing if already ON.

```
Request:   GET /on HTTP/1.0
Response:  ON
```

### `GET /off` — turn compressor OFF
Idempotent.

```
Request:   GET /off HTTP/1.0
Response:  OFF
```

### `GET /toggle` — flip the state
Not idempotent — **never retry** this endpoint on network errors.

```
Request:   GET /toggle HTTP/1.0
Response:  ON        (whichever state resulted)
```

### `GET /status` — read current state
Fast, read-only.

```
Request:   GET /status HTTP/1.0
Response:  ON        (or OFF)
```

---

## Integration guidelines

### Timeouts

- **`/on`, `/off`, `/toggle`** — up to **~2 seconds** because the StamPLC
  physically actuates a servo (1 s) + plays a chime + broadcasts ESP-NOW
  (~0.6 s) before returning.
  **Set client timeout to at least 3 s.**
- **`/status`** — responds in ~50–100 ms (just a memory read).

### Response body

Plain text, no quotes, no trailing newline:
```
ON
```
or
```
OFF
```

### Idempotency & retries

- `/on` and `/off` are **safe to retry** on network errors — they check current
  state and do nothing if already in target.
- `/toggle` is **NOT idempotent** — retrying will flip the state again. Prefer
  `/on` and `/off` for automated integrations.

---

## Examples

### curl
```bash
curl -m 3 http://192.168.3.75/on
curl -m 3 http://192.168.3.75/off
curl -m 2 http://192.168.3.75/status
```

### Python (`requests`)
```python
import requests
BASE = 'http://192.168.3.75'

def compressor_on():
    return requests.get(f'{BASE}/on', timeout=3).text.strip() == 'ON'

def compressor_off():
    return requests.get(f'{BASE}/off', timeout=3).text.strip() == 'OFF'

def compressor_status():
    return requests.get(f'{BASE}/status', timeout=2).text.strip()
```

### Node.js (fetch, Node 18+)
```javascript
const BASE = 'http://192.168.3.75';

async function compressorOn() {
  const r = await fetch(`${BASE}/on`, { signal: AbortSignal.timeout(3000) });
  return (await r.text()).trim() === 'ON';
}

async function compressorStatus() {
  const r = await fetch(`${BASE}/status`, { signal: AbortSignal.timeout(2000) });
  return (await r.text()).trim();
}
```

### PHP
```php
function compressorOn() {
    $ctx = stream_context_create(['http' => ['timeout' => 3]]);
    return trim(file_get_contents('http://192.168.3.75/on', false, $ctx)) === 'ON';
}
```

### Bash / cron
```bash
# Turn on every weekday at 08:00
0 8 * * 1-5 curl -s -m 3 http://192.168.3.75/on

# Log status every minute
* * * * * echo "$(date) $(curl -s -m 2 http://192.168.3.75/status)" >> /var/log/compressor.log
```

---

## Additional endpoints

### `GET /time` — device clock
Returns JSON with both adjusted (local) and raw RTC time plus current TZ offset.

```json
{
  "year": 2026, "month": 5, "day": 27,
  "wd": 2, "h": 21, "m": 7, "s": 38,
  "raw_year": 2026, "raw_month": 5, "raw_day": 27,
  "raw_wd": 2, "raw_h": 20, "raw_m": 7, "raw_s": 38,
  "offset": 1
}
```
Weekday convention: **0 = Monday**, 6 = Sunday.

### `GET /tz/set?offset=N` — set timezone offset
`N` = hours from RTC to local (typical range `-12` … `+14`). Saved to flash.

### `GET /schedule` — full schedule
```json
{
  "off_permanent": [
    {"time": "12:55", "days": 31},
    {"time": "17:55", "days": 31}
  ],
  "off_user": [{"time": "20:00", "days": 31}],
  "on_user":  [{"time": "08:30", "days": 31}]
}
```
`days` is a 7-bit mask: bit 0 = Mon, … bit 6 = Sun. `31` = Mon-Fri,
`96` = weekends, `127` = every day.

### `GET /schedule/add?type=off&time=HH:MM&days=N`
Add a user schedule entry. `type` is `off` or `on`. Adding the same time again
updates the day mask.

### `GET /schedule/del?type=off&time=HH:MM`
Delete a user schedule entry. Permanent OFF entries cannot be deleted.

### `GET /servo/<angle>` — direct servo drive (debug)
Move the servo indicator to `0`…`180` degrees. Used for calibration only.

---

## Under the hood — what happens on `/on`

1. Servo drives to 60° (indicator points at "ON" label) — 1 s
2. Chime plays through the buzzer — ~0.5 s
3. LCD turns green with **ON**
4. ESP-NOW broadcasts state ×4 to peers (Atom Matrix, etc.) — ~0.6 s
5. **GPIO 41 → LOW → SSR conducts → compressor gets 220 V**
6. HTTP responds with `ON`

The "noisy actions first, SSR flip last" ordering makes the high-current
compressor switch happen against a settled, quiet DC rail — no glitches on
the SSR control input.

`/off` runs the same steps in reverse: SSR opens first (compressor immediately
cut), then servo returns to 0° (indicator to "OFF"), sound, ESP-NOW.

---

## Minimum for integration

| Action | URL | Timeout |
|---|---|---|
| Turn ON | `GET http://<ip>/on` | 3 s |
| Turn OFF | `GET http://<ip>/off` | 3 s |
| Read state | `GET http://<ip>/status` | 2 s |

No headers, no tokens, no JSON bodies — just plain GET and a text `ON` / `OFF`
reply. Five lines of code in any language.
