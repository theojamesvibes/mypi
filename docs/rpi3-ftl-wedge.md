# Diagnosing the RPi3 `pihole-FTL` TLS-handshake wedge

## Synopsis

On 2026-04-23 MyPi was logging bursts of `ConnectError` and occasional
`RemoteProtocolError: Server disconnected without sending a response` against
`pihole4`, a Raspberry Pi 3B running Pi-hole v6 (FTL v6.6) at
`192.168.66.22`. A sibling instance `pihole3` (Raspberry Pi 5, same FTL build,
same LAN, same MyPi client code) was rock solid.

Over 24 h: **85 poll failures on the RPi3 vs 8 on the RPi5**, heavily
clustered in bursts. In the middle of the session we caught a **5-minute
sustained wedge** (02:45 → 02:50 UTC, 2026-04-24) where MyPi saw every poll
time out with `ConnectTimeout: _ssl.c:993: The handshake operation timed out`.

The short version of what we found:

- `pihole-FTL` on the RPi3 intermittently stops servicing its HTTPS listener.
- The kernel still completes the TCP three-way handshake into the listen
  backlog, so **any SYN-only probe (`nc -z`) looks fine.**
- FTL never accept()s the socket or starts TLS, so **anything that actually
  tries to make a request (`curl`, `httpx`, browser) times out at the TLS
  handshake stage.**
- The RPi5 under an A/B-identical config never reproduced it.

Recovery: `sudo systemctl restart pihole-FTL` on the affected Pi. Durable
fix: migrate off the RPi3.

---

## The A/B that made it diagnosable

Two Pi-hole v6 instances, **same** FTL version (6.6), **same** MyPi (1.9.4
pre-refactor), **same** LAN segment, **same** MyPi client code paths. The
only variable was the Pi model. Zero-DNS-load on the RPi3 (it was receiving
only its own internal queries) ruled out "load-induced" and "DNS pressure
bleeds into webserver" hypotheses upfront.

Lesson for the next run with another RPi3: keep the hot spare idle (no DNS
traffic) and let MyPi's normal polling be the only load — that isolates the
wedge to FTL's HTTPS path, not the DNS path.

---

## Step-by-step diagnostic procedure

Run these in the order below. Each step is designed to either rule in or
rule out a layer.

### Step 1 — Baseline: confirm MyPi is the only thing reporting trouble

On the MyPi host:

```bash
cd /git/repos/compose/mypi
docker compose logs mypi --since 24h --no-log-prefix 2>&1 \
  | grep -E "Failed to poll.*piholeN" \
  | grep -oE "ConnectError.*|SSL.*|Timeout.*|Read.*|Remote.*" \
  | sort | uniq -c
```

Look at error-class distribution. Bare `ConnectError:` dominating
(empty message) points at a pre-response failure — either TCP or TLS.
`SSLV3_ALERT_HANDSHAKE_FAILURE` / `SSL handshake operation timed out`
points explicitly at TLS.

Sample from this session:

```
 73 ConnectError:
  4 SSL handshake / record-layer failures
  2 ReadError:
```

### Step 2 — Pi-hole-side health check

On the suspect Pi:

```bash
# Recent FTL errors
sudo journalctl -u pihole-FTL --since "3 hours ago" \
  | grep -iE "warn|error|restart|reload|signal"

# Confirm FTL version + listener
pihole-FTL --version
sudo ss -lntp | grep 443

# NIC / link health
dmesg -T | grep -iE "eth|link|usb|smsc|lan78" | tail -30
```

Expected when the host itself is fine (as was the case for us): empty
journalctl grep, FTL listening on :443, no `Link is Down` events since
boot. This rules out an FTL crash, NIC flap, or lighttpd-style webserver.
It does **not** rule out an FTL internal stall — that's invisible here.

> Pi-hole v6 embeds its HTTPS server in `pihole-FTL` (civetweb-based).
> There is no lighttpd process. Do not look at lighttpd logs.

### Step 3 — Three-probe correlation (the key step)

Start three loops simultaneously and let them run ~10–15 minutes, long
enough to catch at least one burst.

**Probe A — TCP-only from the MyPi host** (answers "is port 443
reachable?")

```bash
while true; do date "+%H:%M:%S"; \
  nc -zv -w 2 <pi-ip> 443 2>&1 | tail -1; sleep 1; done
```

**Probe B — Real HTTPS request from the MyPi host** (answers "does
FTL actually respond?")

```bash
while true; do date; \
  curl -sk -o /dev/null -w "http_code=%{http_code} time=%{time_total}\n" \
  --max-time 5 https://<pi-ip>/api/info/version; sleep 5; done
```

**Probe C — Same request from inside the MyPi container** (answers
"does Docker networking add anything?")

```bash
cd /git/repos/compose/mypi
docker compose exec mypi sh -c 'while true; do date; \
  python3 -c "import httpx; r = httpx.get(\"https://<pi-ip>/api/info/version\", verify=False, timeout=5); print(\"OK\", r.status_code)" \
  2>&1 | tail -1; sleep 5; done'
```

**Optional Probe D — ss loop on the Pi itself** (shows whether the
kernel is accepting connections that FTL never picks up)

```bash
while true; do date "+%H:%M:%S"; \
  sudo ss -tan state established '( sport = :443 )'; sleep 10; done
```

### Step 4 — Wait for a burst, then read the correlation matrix

When MyPi's collector logs `Failed to poll ... piholeN`, line the
exact second up against the four probes. The result tells you which
layer:

| Probe A (`nc -z`) | Probe B (`curl`) | Probe C (container `httpx`) | Conclusion |
|---|---|---|---|
| FAIL | FAIL | FAIL | Network / link (check Probe D on the Pi; check switches) |
| OK | FAIL | FAIL | **FTL-side wedge** — kernel accepting, FTL not responding |
| OK | OK | FAIL | Docker networking / NAT — look at conntrack, bridge, MTU |
| OK | OK | OK | MyPi's in-process state — stale SID, httpx pool, asyncio |

Our 2026-04-23 session landed in row 2: `nc` open, `curl` timing out
at 5.002 s, container httpx returning `ConnectTimeout: _ssl.c:993: The
handshake operation timed out`, MyPi collector logging `ConnectTimeout`
on the same seconds. **Straightforward FTL wedge.**

### Step 5 — Confirm it's the TLS layer specifically

If row 2 is what you see, the error type from Probe C narrows it further:

- `_ssl.c:993: The handshake operation timed out` → FTL's civetweb
  never started TLS on the accepted socket. That's the wedge signature.
- `SSLV3_ALERT_HANDSHAKE_FAILURE` / `record layer failure` → TLS
  started but aborted mid-handshake. Same layer, different internal
  state.
- `Server disconnected without sending a response` /
  `RemoteProtocolError` → TLS completed, request wrote, then FTL
  closed the socket before a response. Still FTL-side, different phase.

All three were observed in this session on the same RPi3, often
within minutes of each other — they're different failure modes of the
same stalled civetweb thread, not separate bugs.

### Step 6 — Recovery

```bash
sudo systemctl restart pihole-FTL
```

On the Pi. DNS blips for ~2 s. HTTPS listener comes back immediately.
The wedge does not self-heal — MyPi's circuit-breaker + client-eviction
layers cleanly absorb short bursts but cannot recover a multi-minute
FTL stall.

---

## Interpretation notes for the next RPi3 test

- **If the second RPi3 reproduces it under the same A/B**, the platform
  is the problem (RPi3 class). Confirms what the 2026-04-23 data
  suggested and closes the question. Recommendation: document as a
  supported-but-not-recommended platform and move on.
- **If the second RPi3 does _not_ reproduce it**, you've got a bad
  specific board (SD card, PSU under-voltage drooping civetweb threads,
  dying USB-Ethernet PHY, etc.). At that point the SD card swap / PSU
  swap / thermal check becomes the next axis to vary.
- **Key negative controls** that held up this time and are worth
  re-running:
  - The RPi5 never reproduced it → rules out FTL v6.6 as universally
    broken.
  - `nc -z` always passed → rules out link-level / cable / switch.
  - Host `curl` and container `httpx` failed identically on the same
    seconds → rules out Docker networking.
  - MyPi's other instances (pihole1, pihole2) weren't affected → rules
    out MyPi-side systemic issues.
- **Things *not* worth chasing** based on this session (ruled out,
  don't repeat the work):
  - Stale SID / `401` handling in MyPi — browser succeeds in-window,
    host curl succeeds out-of-window; MyPi's auth is fine.
  - httpx connection-pool poisoning — fresh connects fail identically
    to reused ones.
  - Teleporter-endpoint-specific civetweb bug — `/api/info/version`
    fails, not just `/api/teleporter`.
  - lighttpd tuning — v6 doesn't use lighttpd.

---

## Artifacts from 2026-04-23 session

Captured verbatim excerpts, for reference when comparing to the next
run:

**MyPi collector during the 5-min wedge:**

```
2026-04-24 02:09:29 WARNING Failed to poll queries for pihole4: RemoteProtocolError: Server disconnected without sending a response.
2026-04-24 02:09:29 WARNING Failed to poll stats for pihole4: ReadError:
2026-04-24 02:10:29 WARNING Failed to poll stats for pihole4: ConnectError:
2026-04-24 02:49:39 WARNING Failed to poll stats for pihole4: ConnectTimeout:
```

**Container httpx during the same window:**

```
httpx.ConnectTimeout: _ssl.c:993: The handshake operation timed out
```

**Host curl during the same window:**

```
http_code=000 time=5.002798
```

**Host nc during the same window:**

```
wtrpihole3.myssdomain.net [192.168.66.22] 443 (https) open
```

Same seconds. Different answers from different layers. That
triangulation is the whole value of the three-probe approach — no one
probe alone would have isolated the wedge to the HTTPS thread in FTL.
