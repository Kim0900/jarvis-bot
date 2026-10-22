#!/usr/bin/env python3
"""TASK#214 design v1.3: numeric checks of the formulas and deadlines the document claims (CASSANDRA #5739 G214-04/05/06).

    python3 docs/design/task214_v13_simulations.py            # about 1 minute; exits 1 if any claim is NOT reproduced

Nothing here touches a database or the network beyond 127.0.0.1.  A: token-bucket cap (G214-04).  B: end-to-end deadline (G214-05).
C: external-report processing latency under the proposed polling policy (G214-06).  Every claim in the design text that carries a number is a
`check(...)` below; the printed values are the ones quoted in the document.
"""
import asyncio, bisect, math, os, random, socket, ssl, subprocess, sys, tempfile, threading, time
from pathlib import Path

results = []
def check(name, ok, info=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)

NS = 1_000_000_000

# ====================================================================================================================== A. token bucket
class Bucket:
    """integer-nanosecond token bucket: capacity B tokens, one token per COST_NS.  level is stored in ns of 'refill time' (no floating point)."""
    def __init__(self, capacity=3, cost_ns=10 * NS, now_ns=0, start_full=True):
        self.cost, self.cap = cost_ns, capacity * cost_ns
        self.level = self.cap if start_full else 0
        self.last = now_ns
    def take(self, now_ns):
        el = max(0, now_ns - self.last); self.last = max(self.last, now_ns)
        self.level = min(self.cap, self.level + el)
        if self.level >= self.cost:
            self.level -= self.cost; return True
        return False

def max_in_window(times, T_ns):
    """largest number of admissions inside any CLOSED window [t, t + T] (two pointers over the admitted times)"""
    best = 0
    for i, t in enumerate(times):
        k = bisect.bisect_right(times, t + T_ns)
        best = max(best, k - i)
    return best

def greedy(duration_s, step_ms=1, start_full=True):
    b = Bucket(start_full=start_full); out = []
    for t in range(0, int(duration_s * 1000) + 1, step_ms):
        if b.take(t * 1_000_000): out.append(t * 1_000_000)
    return out

print("== A. token bucket (B=3, one token per 10 s), greedy offered load, 1 ms resolution, 1 hour ==")
bound = lambda T_s: 3 + math.floor(T_s / 10)
full = greedy(3600, 1, True); empty = greedy(3600, 1, False)
rows = []
for T in (1, 10, 59.999, 60, 90, 600, 3600):
    mf, me = max_in_window(full, int(T * NS)), max_in_window(empty, int(T * NS))
    rows.append((T, mf, me, bound(T)))
    print(f"   window {T:>8} s: starts with a FULL bucket -> max {mf:3d} | starts EMPTY -> max {me:3d} | formula 3+floor(T/10) = {bound(T)}")
check("A1 formula N(T) <= 3 + floor(T/10) holds for every window length and is TIGHT (reached) when the bucket is full at the window start",
      all(mf <= b_ and me <= b_ and mf == b_ for _, mf, me, b_ in rows), str([(T, mf) for T, mf, _, _ in rows]))
m60 = max_in_window(full, 60 * NS)
check("A2 the v1.2 claim 'at most 6 per minute' is FALSE: a full bucket admits 9 inside one 60 s window (3 burst + 6 refills)", m60 == 9, f"observed {m60}")
check("A3 steady state (bucket already drained) gives 7 in a closed 60 s window, long-run average 6 per minute", max_in_window(empty, 60 * NS) == 7 and abs(len(empty) - 360) <= 1, f"{max_in_window(empty, 60 * NS)} / {len(empty)} per hour")
check("A4 1-second and 10-second boundaries: 3 within 1 s, 4 within a closed 10 s window", max_in_window(full, 1 * NS) == 3 and max_in_window(full, 10 * NS) == 4)
check("A5 exactly at the 60 s / 10 s edges: a window of 59.999 s holds 8, of 60 s holds 9 (the 6th refill lands exactly on t0+60 s)", max_in_window(full, int(59.999 * NS)) == 8 and m60 == 9)
b = Bucket(now_ns=0); [b.take(0) for _ in range(3)]
check("A6 integer arithmetic at the refill instant: 9.999999999 s after draining -> refused, exactly 10.000000000 s -> granted, then refused again",
      (not b.take(9_999_999_999)) and b.take(10 * NS) and (not b.take(10 * NS)))
rnd = random.Random(214); worst = {}
for seed in range(300):
    b2 = Bucket(); t = 0; ad = []
    while t < 600 * NS:
        t += int(rnd.expovariate(1 / (rnd.choice((0.05, 0.5, 2.0, 9.0)) )) * NS)
        if b2.take(t): ad.append(t)
    for T in (1, 10, 60):
        worst[T] = max(worst.get(T, 0), max_in_window(ad, T * NS))
check("A7 property test: 300 random arrival sequences (10 min each, mixed rates) never exceed the formula", all(worst[T] <= bound(T) for T in worst), str(worst))
lock = threading.Lock(); bk = Bucket(now_ns=time.monotonic_ns()); adm = []
stop = time.monotonic() + 12
def hammer():
    while time.monotonic() < stop:
        with lock:
            n = time.monotonic_ns()
            if bk.take(n): adm.append(n)
ths = [threading.Thread(target=hammer) for _ in range(8)]; t0 = time.monotonic_ns(); [t.start() for t in ths]; [t.join() for t in ths]
span = (adm[-1] - adm[0]) / NS if adm else 0
check("A8 8 threads hammering one lock-protected bucket for 12 s: admissions <= 3 + floor(12/10) = 4, none duplicated by races", 0 < len(adm) <= 4 and len(set(adm)) == len(adm), f"{len(adm)} admitted over {span:.1f} s")
bk60 = Bucket(capacity=3, cost_ns=60 * NS); ad60 = [t * 100_000_000 for t in range(0, 36001) if bk60.take(t * 100_000_000)]
check("A10 wake bucket (burst 3, one token per 60 s): N(T) <= 3 + floor(T/60); 1 hour -> 63, one day average 1,440 (+3 burst)",
      all(max_in_window(ad60, T * NS) <= 3 + T // 60 for T in (1, 60, 600, 3600)) and max_in_window(ad60, 3600 * NS) == 63, str([max_in_window(ad60, T * NS) for T in (1, 60, 600, 3600)]))
print(f"   corrected circuit-OPEN bound for an interval of L seconds: user requests <= 3 + floor(L/10)  (+ probes <= floor(L/60)+1); e.g. L=60 -> 9, L=600 -> 63, L=3600 -> 363")
check("A9 the corrected interval formula holds on the 1-hour greedy run for L = 60 / 600 / 3600", [max_in_window(full, L * NS) for L in (60, 600, 3600)] == [9, 63, 363])

# ====================================================================================================================== B. end-to-end deadline
print("\n== B. five-second end-to-end deadline vs per-phase timeouts (real local TLS server, httpx) ==")
import httpx
tmp = Path(tempfile.mkdtemp(prefix="t214sim_")); crt, key = tmp / "c.pem", tmp / "k.pem"
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-days", "1", "-keyout", str(key), "-out", str(crt),
                "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(crt, key)
cctx = ssl.create_default_context(cafile=str(crt))

class Server:
    """accepts TCP at once; per request: wait hs_delay before the TLS handshake, resp_delay before the response, optionally drip the body"""
    def __init__(self):
        self.ls = socket.socket(); self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); self.ls.bind(("127.0.0.1", 0)); self.ls.listen(50)
        self.port = self.ls.getsockname()[1]; self.cfg = dict(hs=0.0, resp=0.0, drip=0.0); self.accepted = 0; self.client_gone = 0; self.conns = []
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while True:
            try: c, _ = self.ls.accept()
            except OSError: return
            self.accepted += 1; threading.Thread(target=self.serve, args=(c, dict(self.cfg)), daemon=True).start()
    def serve(self, c, cfg):
        try:
            time.sleep(cfg["hs"]); s = sctx.wrap_socket(c, server_side=True); self.conns.append(s)
            data = s.recv(65536)
            if not data: return
            time.sleep(cfg["resp"])
            s.setblocking(False)
            try:
                if s.recv(1) == b"":
                    self.client_gone += 1; return          # peer closed while we were still 'working'
            except ssl.SSLWantReadError:
                pass
            except (ssl.SSLEOFError, ConnectionError, OSError):
                self.client_gone += 1; return
            s.setblocking(True)
            if cfg["drip"]:
                s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
                for _ in range(20):
                    time.sleep(cfg["drip"]); s.sendall(b"x")
            else:
                s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            s.settimeout(1.0)
            try: s.recv(1)
            except Exception: pass
        except Exception:
            self.client_gone += 1
        finally:
            try: c.close()
            except Exception: pass
SRV = Server(); URL = f"https://localhost:{SRV.port}/x"

async def timed(coro_factory, deadline=None):
    t0 = time.monotonic(); outcome = "ok"
    try:
        if deadline:
            async with asyncio.timeout(deadline):
                await coro_factory()
        else:
            await coro_factory()
    except TimeoutError: outcome = "deadline"
    except httpx.TimeoutException as e: outcome = type(e).__name__
    except Exception as e: outcome = type(e).__name__
    return time.monotonic() - t0, outcome

async def scenario_b():
    PH = httpx.Timeout(connect=3, read=5, write=5, pool=5)          # the v1.2 text: 'connect 3 s + read 5 s'
    # B1 slow TLS handshake + slow first byte: every phase is inside its own limit, the SUM is not
    SRV.cfg.update(hs=2.8, resp=4.8, drip=0.0)
    async with httpx.AsyncClient(timeout=PH, verify=cctx) as c:
        t_no, o_no = await timed(lambda: c.get(URL))
    check("B1 per-phase timeouts alone do NOT bound the total: handshake 2.8 s (<3) + response 4.8 s (<5) took more than 5 s and succeeded", t_no > 5.0 and o_no == "ok", f"{t_no:.2f} s outcome={o_no}")
    async with httpx.AsyncClient(timeout=PH, verify=cctx) as c:
        t_dl, o_dl = await timed(lambda: c.get(URL), deadline=5.0)
    check("B2 one monotonic end-to-end deadline (asyncio.timeout 5.0 s around the whole request) ends the same request at 5.0 s", o_dl == "deadline" and 4.9 <= t_dl <= 5.3, f"{t_dl:.2f} s")
    for _ in range(100):
        if SRV.client_gone >= 1: break
        await asyncio.sleep(0.1)
    check("B3 cancellation closes the connection: the server sees the client go away", SRV.client_gone >= 1, f"client_gone={SRV.client_gone}")
    # B4 pool wait is part of the budget
    SRV.cfg.update(hs=0.0, resp=4.0, drip=0.0)
    async with httpx.AsyncClient(timeout=PH, verify=cctx, limits=httpx.Limits(max_connections=1)) as c:
        async def two():
            a = asyncio.create_task(c.get(URL)); await asyncio.sleep(0.1)
            return await c.get(URL)
        t_pool, o_pool = await timed(two)
    async with httpx.AsyncClient(timeout=PH, verify=cctx, limits=httpx.Limits(max_connections=1)) as c:
        async def two_dl():
            a = asyncio.create_task(c.get(URL)); await asyncio.sleep(0.1)
            return await c.get(URL)
        t_pool_dl, o_pool_dl = await timed(two_dl, deadline=5.0)
    check("B4 pool wait counts: the second request waits ~3.9 s for the single pooled connection and then needs 4 s -> 8-9 s without a deadline (it may even end as ReadTimeout), exactly the deadline with one",
          t_pool > 7.5 and o_pool_dl == "deadline" and 4.9 <= t_pool_dl <= 5.3, f"no deadline {t_pool:.2f} s ({o_pool}), with deadline {t_pool_dl:.2f} s")
    # B5 slow-drip body: every read is inside the read timeout, the body never ends
    SRV.cfg.update(hs=0.0, resp=0.0, drip=1.0)
    async with httpx.AsyncClient(timeout=PH, verify=cctx) as c:
        t_drip, o_drip = await timed(lambda: c.get(URL))
    async with httpx.AsyncClient(timeout=PH, verify=cctx) as c:
        t_drip_dl, o_drip_dl = await timed(lambda: c.get(URL), deadline=5.0)
    check("B5 slow-drip body (1 byte per second): the read timeout never fires (20 s total); the end-to-end deadline cuts it at 5.0 s",
          t_drip > 15 and o_drip == "ok" and o_drip_dl == "deadline" and 4.9 <= t_drip_dl <= 5.3, f"no deadline {t_drip:.1f} s, with deadline {t_drip_dl:.2f} s")
    # B6 deadline shared by the whole user call, including the semaphore/budget wait that comes BEFORE the request
    sem = asyncio.Semaphore(2)
    SRV.cfg.update(hs=0.0, resp=3.0, drip=0.0)
    async with httpx.AsyncClient(timeout=PH, verify=cctx) as c:
        async def call():
            async with asyncio.timeout(5.0):                       # deadline starts BEFORE admission: semaphore wait + request share it
                async with sem: await c.get(URL)
        t0 = time.monotonic(); res = await asyncio.gather(*[timed(call) for _ in range(4)])
    outs = sorted(o for _, o in res); worst = max(t for t, _ in res)
    check("B6 4 concurrent user calls, concurrency limit 2, each takes 3 s: the two that must queue hit the SAME 5 s deadline instead of 3+3 = 6 s", outs == ["deadline", "deadline", "ok", "ok"] and worst <= 5.3, f"outcomes {outs}, slowest {worst:.2f} s")

asyncio.run(scenario_b())

# thread (sync) path: the caller's latency can be bounded with wait_for, the thread's occupation cannot be cancelled
SRV.cfg.update(hs=2.8, resp=4.8, drip=0.0)
def sync_get():
    with httpx.Client(timeout=httpx.Timeout(connect=3, read=5, write=5, pool=5), verify=cctx) as c: return c.get(URL).status_code
async def thread_case():
    t0 = time.monotonic(); th_done = {}
    def wrapped():
        r = sync_get(); th_done["t"] = time.monotonic() - t0; return r
    try: await asyncio.wait_for(asyncio.to_thread(wrapped), 5.0); out = "ok"
    except TimeoutError: out = "caller released"
    t_caller = time.monotonic() - t0
    while "t" not in th_done and time.monotonic() - t0 < 12: await asyncio.sleep(0.1)
    return t_caller, th_done.get("t"), out
t_caller, t_thread, out = asyncio.run(thread_case())
check("B7 (evidence only; DB calls may not use threads since v1.4) sync/thread path: wait_for returns the CALLER at 5.0 s, but the thread keeps working until its own per-phase limits end (~7.7 s): the concurrency permit must be released when the THREAD ends, not at the deadline",
      out == "caller released" and 4.9 <= t_caller <= 5.3 and t_thread and t_thread > 6.0, f"caller {t_caller:.2f} s, thread occupied {t_thread and round(t_thread, 2)} s")

# ====================================================================================================================== C. report latency under the polling policy
print("\n== C. external atlas-report processing latency (policy simulation, deterministic clock) ==")
P, PAGE = 300.0, 3
def next_delay(n_fail, u):
    """v1.3 rule: jitter only SHORTENS the delay: delay = min(P * 2**n, 600) * (1 - 0.2 * u), u in [0,1)"""
    return min(P * 2 ** n_fail, 600.0) * (1 - 0.2 * u)

rnd = random.Random(2140)
lo = min(next_delay(0, u / 1000) for u in range(1000)); hi = max(next_delay(0, u / 1000) for u in range(1000))
hi_b = max(next_delay(5, u / 1000) for u in range(1000))
check("C1 delay with jitter that only shortens: normal polls in (240, 300] s, backoff never above 600 s (the v1.2 +-20% jitter allowed up to 360 s / 720 s)", lo > 239.9 and hi <= 300.0 and hi_b <= 600.0, f"normal {lo:.1f}..{hi:.1f} s, backoff cap {hi_b:.1f} s")

def simulate(K, drain, lost_wake=True, trials=2000, outage=None):
    """K reports appear at random instants in one 300 s span (status=pending, limit=3, oldest first).  A poll takes PAGE reports; with `drain`
    a full page triggers a continuation poll 1 s later (up to 10 pages per cycle, then 30 s).  Returns the worst 'time until PROCESSING STARTS'."""
    worst = 0.0
    for _ in range(trials):
        arr = sorted(rnd.uniform(0, 300) for _ in range(K)); phase = rnd.uniform(0, P)        # next poll after the first arrival
        t = arr[0] + rnd.uniform(0, P) if not lost_wake else phase
        queue = list(arr); started = []; polls = 0
        t = max(t, 0.0)
        while queue or started == []:
            avail = [a for a in queue if a <= t]
            if avail:
                take = avail[:PAGE]; [queue.remove(a) for a in take]; started += [(a, t) for a in take]; polls += 1
                if drain and len(take) == PAGE: t += 1.0 if polls % 10 else 30.0; continue
                t += rnd.uniform(0.8 * P, P) if not queue else rnd.uniform(0.8 * P, P)
            else:
                t += rnd.uniform(0.8 * P, P) if not queue else 1.0
            if not queue: break
        worst = max(worst, max(s - a for a, s in started))
    return worst
for K in (1, 3, 10, 30):
    nd, dr = simulate(K, False), simulate(K, True)
    print(f"   K={K:2d} queued reports, wake LOST: worst start delay without drain {nd:7.1f} s | with drain {dr:7.1f} s")
w_no_drain_10, w_drain_10, w_drain_30 = simulate(10, False), simulate(10, True), simulate(30, True)
check("C2 limit=3 WITHOUT a drain loop breaks the 5-minute claim: 10 queued reports + a lost wake -> some report waits more than 15 minutes", w_no_drain_10 > 900, f"{w_no_drain_10:.0f} s")
check("C3 WITH the drain loop (continuation poll after 1 s while a full page was returned) up to 30 queued reports start within 300 s + page spacing", w_drain_10 <= 300 + 10 and w_drain_30 <= 300 + 10 * 1 + 30 + 10, f"K=10 {w_drain_10:.0f} s, K=30 {w_drain_30:.0f} s")
# outage: backoff n>=1 means the 5-minute statement does not apply; after recovery a catch-up poll is run at once
def after_recovery(drain_catchup):
    probe_gap = 600.0; worst = 0
    for _ in range(2000):
        close_at = rnd.uniform(0, probe_gap)             # circuit closes at the (random) next probe success
        poll_after = 0.0 if drain_catchup else rnd.uniform(0.8 * 600, 600)
        worst = max(worst, close_at + poll_after)
    return worst
w_with, w_without = after_recovery(True), after_recovery(False)
check("C4 after an outage the first poll happens at the moment the circuit closes (catch-up poll): worst wait <= one probe gap (600 s); without it up to ~1200 s", w_with <= 600.0 and w_without > 900, f"with catch-up {w_with:.0f} s, without {w_without:.0f} s")


# ====================================================================================================================== D. v1.4 (G214-07, G214-08)
print("\n== D. absolute deadline with forced termination, isolated permit, single formula (v1.4) ==")
import ast, re as _re
DOC = Path(__file__).resolve().parent / "task214_amplification_v1.md"
doc_text = DOC.read_text(encoding="utf-8")
HISTORY = ("거짓", "틀렸", "제거", "정정", "v1.2", "v1.3")      # a line that QUOTES the old claim in order to correct it is history, not a claim
stale = [pat for pat in ("6건/분", "6×60", "분당 6건 ×") for l in doc_text.splitlines() if pat in l and not any(h in l for h in HISTORY)]
five = [l for l in doc_text.splitlines() if l.lstrip().startswith("- **⑤")]
check("D1 (G214-08) the design text has no stale per-minute cap ('6건/분', '6×60') and target ⑤ is written with the single interval formula U(L)+P(L)",
      not stale and len(five) == 1 and "U(L)" in five[0] and "P(L)" in five[0], f"stale={stale} five={len(five)}")
U = lambda L: 3 + L // 10
Pf = lambda L: L // 60 + 1
check("D1b the single formula evaluates to the numbers used elsewhere in the document: U(60)=9, U(600)=63, U(3600)=363, P(3600)=61, total 424 for one hour",
      (U(60), U(600), U(3600), Pf(3600), U(3600) + Pf(3600)) == (9, 63, 363, 61, 424))

DEADLINE, GRACE, CAP, SLACK = 5.0, 0.2, 2, 0.08      # SLACK: allowed caller overshoot (timer + scheduling jitter), NOT cleanup time
class Guard:
    """reference model of the v1.4/v1.5 contract.  Admission is NON-blocking (over the cap -> reject, zero DB calls).  The absolute deadline starts at
    admission and is enforced by a TIMER (loop.call_later), not by awaiting the task: when it fires the CALLER is released at once (deadline+scheduling
    jitter only) and the task is cancelled in the background.  The permit is returned by the task's done-callback, i.e. when the task REALLY ended;
    a task still alive GRACE seconds after the cancel is counted as stuck (also by timer, never by the caller).  A result that arrives after the
    deadline is discarded (counted), an exception is always retrieved (no 'never retrieved' warnings)."""
    def __init__(self, cap=CAP, deadline=None, grace=None):
        self.cap, self.deadline, self.grace = cap, DEADLINE if deadline is None else deadline, GRACE if grace is None else grace
        self.active, self.stuck, self.max_active, self.rejected, self.late_discarded, self.stuck_total, self.settled_twice = {}, set(), 0, 0, 0, 0, 0
    async def call(self, factory):
        if len(self.active) >= self.cap:
            self.rejected += 1; return "rejected", 0.0
        loop = asyncio.get_running_loop(); t0 = loop.time(); fut = loop.create_future()
        def settle(kind):
            if fut.done(): self.settled_twice += 1; return
            fut.set_result((kind, loop.time() - t0))
        task = asyncio.ensure_future(factory()); self.active[task] = t0; self.max_active = max(self.max_active, len(self.active))
        def on_done(t):
            self.active.pop(t, None); self.stuck.discard(t)
            exc = None if t.cancelled() else t.exception()                       # always retrieved
            if fut.done():
                if not t.cancelled() and exc is None: self.late_discarded += 1   # finished after the deadline: result thrown away
                return
            settle("ok" if (not t.cancelled() and exc is None) else "error:" + (type(exc).__name__ if exc else "Cancelled"))
        def check_stuck():
            if not task.done(): self.stuck.add(task); self.stuck_total += 1
        def on_deadline():
            if fut.done(): return
            settle("deadline"); task.cancel(); loop.call_later(self.grace, check_stuck)
        task.add_done_callback(on_done)
        timer = loop.call_later(self.deadline, on_deadline)
        try: return await fut
        finally: timer.cancel()

class RawTLS:
    """raw TCP server for adversarial server behaviour: mode 'hsdrip' (valid TLS record header then 1 byte/s), 'hdrdrip' (TLS ok, status line then 1 header byte/s), 'silent'"""
    def __init__(self, mode):
        self.mode, self.accepted, self.closed = mode, 0, 0
        self.ls = socket.socket(); self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); self.ls.bind(("127.0.0.1", 0)); self.ls.listen(50)
        self.port = self.ls.getsockname()[1]; threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while True:
            try: c, _ = self.ls.accept()
            except OSError: return
            self.accepted += 1; threading.Thread(target=self.serve, args=(c,), daemon=True).start()
    def serve(self, c):
        try:
            if self.mode == "silent":
                c.settimeout(60)
                while c.recv(65536): pass
                return
            if self.mode == "hsdrip":
                c.recv(65536); c.sendall(b"\x16\x03\x03\x00\xff")
                for _ in range(200): time.sleep(1.0); c.sendall(b"\x00")
                return
            if self.mode == "hdrdrip":
                s = sctx.wrap_socket(c, server_side=True); s.recv(65536); s.sendall(b"HTTP/1.1 200 OK\r\n")
                for _ in range(200): time.sleep(1.0); s.sendall(b"X")
        except Exception:
            self.closed += 1
        finally:
            try: c.close()
            except Exception: pass

async def adversary(name, url, wait_close=lambda: True):
    g = Guard()
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3, read=5, write=5, pool=5), verify=cctx) as c:
        t0 = time.monotonic(); res, el = await g.call(lambda: c.get(url)); t_ret = time.monotonic() - t0
        t1 = time.monotonic()
        while g.active and time.monotonic() - t1 < 3: await asyncio.sleep(0.01)
        t_perm = time.monotonic() - t1
    return res, t_ret, t_perm, g

async def scenario_d():
    SRV.cfg.update(hs=0.0, resp=0.0, drip=1.0); g0 = SRV.client_gone
    res, t_ret, t_perm, g = await adversary("body drip", URL)
    check("D2 (G214-07) body drip 1 byte/s: caller released at the 5.0 s absolute deadline, permit back <0.3 s later (task really ended)", res == "deadline" and 4.9 <= t_ret <= DEADLINE + SLACK and t_perm < 0.3 and not g.active and not g.stuck, f"caller {t_ret:.2f} s, permit +{t_perm:.2f} s")
    SRV.cfg.update(hs=0.0, resp=0.0, drip=0.0)
    hd = RawTLS("hdrdrip"); res, t_ret, t_perm, g = await adversary("header drip", f"https://localhost:{hd.port}/x")
    check("D3 response HEADER drip (one byte per second, read timeout never fires): absolute deadline holds, permit released", res == "deadline" and 4.9 <= t_ret <= DEADLINE + SLACK and t_perm < 0.3 and not g.active, f"caller {t_ret:.2f} s, permit +{t_perm:.2f} s")
    hs = RawTLS("hsdrip"); res, t_ret, t_perm, g = await adversary("handshake drip", f"https://localhost:{hs.port}/x")
    check("D4 TLS HANDSHAKE drip (valid record header, then one byte per second): the call ends inside the deadline (here the 3 s connect timeout fires first; the 5.0 s deadline is the backstop), permit released", res in ("deadline", "error:ConnectTimeout", "error:ReadTimeout") and t_ret <= DEADLINE + SLACK and t_perm < 0.3 and not g.active, f"{res}: caller {t_ret:.2f} s, permit +{t_perm:.2f} s")
    sl = RawTLS("silent"); res, t_ret, t_perm, g = await adversary("silent", f"https://localhost:{sl.port}/x")
    check("D5 TCP accepted but never answered: the call ends inside the deadline (the 3 s connect timeout fires first; the 5.0 s deadline is the backstop), permit released", res in ("deadline", "error:ConnectTimeout", "error:ReadTimeout") and t_ret <= DEADLINE + SLACK and t_perm < 0.3 and not g.active, f"{res}: caller {t_ret:.2f} s, permit +{t_perm:.2f} s")
    # saturation: 50 simultaneous user calls against a draining server, cap 2
    SRV.cfg.update(hs=0.0, resp=0.0, drip=1.0); acc0 = SRV.accepted; g = Guard()
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3, read=5, write=5, pool=5), verify=cctx, limits=httpx.Limits(max_connections=4)) as c:
        t0 = time.monotonic(); out = await asyncio.gather(*[g.call(lambda: c.get(URL)) for _ in range(50)])
        wave1 = time.monotonic() - t0
        while g.active and time.monotonic() - t0 < 8: await asyncio.sleep(0.01)
    rej = sum(1 for r, _ in out if r == "rejected"); dl = sum(1 for r, _ in out if r == "deadline")
    check("D6 50 simultaneous calls, cap 2, hostile (dripping) server: 48 rejected with ZERO server connections, 2 reach the server, never more than 2 in flight, all callers back within the deadline+grace",
          rej == 48 and dl == 2 and SRV.accepted - acc0 == 2 and g.max_active == 2 and wave1 <= DEADLINE + SLACK + 0.1, f"rejected {rej}, deadline {dl}, server accepted {SRV.accepted - acc0}, max in flight {g.max_active}, {wave1:.2f} s")
    # task that ignores cancellation: caller released AT the deadline, cleanup/permit decoupled, stuck flagged later by timer, admission fails closed, recovers
    g = Guard()
    async def stubborn():
        end = time.monotonic() + 9.0
        while time.monotonic() < end:
            try: await asyncio.sleep(end - time.monotonic())
            except asyncio.CancelledError: continue
    t0 = time.monotonic(); r12 = await asyncio.gather(g.call(stubborn), g.call(stubborn)); t_back = time.monotonic() - t0
    stuck_at_return = len(g.stuck); await asyncio.sleep(GRACE + 0.1); n_stuck = len(g.stuck)
    t1 = time.monotonic(); r3 = await g.call(stubborn); t_rej = time.monotonic() - t1
    while g.active and time.monotonic() - t0 < 14: await asyncio.sleep(0.05)
    r4, _ = await g.call(lambda: asyncio.sleep(0.01))
    check("D7 (G214-09) tasks that IGNORE cancellation: callers back AT the deadline (<= 5.0 s + 80 ms, no grace wait), not yet flagged stuck at that instant, flagged stuck 0.2 s later by timer, a third call is rejected in <50 ms with zero DB work, admission recovers when the tasks really end",
          [x[0] for x in r12] == ["deadline", "deadline"] and DEADLINE - 0.01 <= t_back <= DEADLINE + SLACK and stuck_at_return == 0 and n_stuck == 2 and r3[0] == "rejected" and t_rej < 0.05 and r4 == "ok" and g.stuck_total == 2,
          f"callers back {t_back:.3f} s, stuck at return {stuck_at_return}, +0.3 s {n_stuck}, third call {t_rej * 1000:.1f} ms {r3[0]}, after recovery {r4}")
    # D10: the 5-second boundary.  A task that ends at deadline +-: before -> ok, after -> deadline at the deadline and the late result is discarded
    g = Guard(cap=4)
    async def ends_at(dt, val="v"):
        end = time.monotonic() + dt
        while time.monotonic() < end:
            try: await asyncio.sleep(max(0.0, end - time.monotonic()))
            except asyncio.CancelledError: continue            # uncooperative, so a result can arrive AFTER the deadline
        return val
    t0 = time.monotonic(); before, after = await asyncio.gather(g.call(lambda: ends_at(DEADLINE - 0.05)), g.call(lambda: ends_at(DEADLINE + 0.05)))
    await asyncio.sleep(0.3)
    check("D10 (G214-09) 5-second boundary: finishing 50 ms BEFORE the deadline -> ok; finishing 50 ms AFTER -> caller already back at the deadline with 'deadline', the late result is DISCARDED (counted), permit returned, not stuck",
          before[0] == "ok" and before[1] < DEADLINE and after[0] == "deadline" and DEADLINE - 0.01 <= after[1] <= DEADLINE + SLACK and g.late_discarded == 1 and not g.active and not g.stuck and g.stuck_total == 0 and g.settled_twice == 0,
          f"before {before[0]}@{before[1]:.3f}, after {after[0]}@{after[1]:.3f}, discarded {g.late_discarded}, settled twice {g.settled_twice}")
    # D11: 300 races exactly on the boundary (scaled deadline 0.4 s, finish at deadline +-2 ms): exactly one outcome per call, caller never later than deadline+SLACK, no unretrieved exceptions
    unretrieved = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unretrieved.append(ctx.get("message")))
    gs = Guard(cap=300, deadline=0.4, grace=0.05); rnd2 = random.Random(2142)
    async def finish_near(off, boom):
        await asyncio.sleep(0.4 + off)
        if boom: raise RuntimeError("boom after/near deadline")
        return "v"
    res = await asyncio.gather(*[gs.call(lambda o=rnd2.uniform(-0.01, 0.01), b=(i % 3 == 0): finish_near(o, b)) for i in range(300)])
    await asyncio.sleep(0.3); import gc; gc.collect(); await asyncio.sleep(0.05)
    kinds = {}
    for k, _ in res: kinds[k.split(":")[0]] = kinds.get(k.split(":")[0], 0) + 1
    over = max(el for _, el in res) - 0.4
    check("D11 (G214-09) 300 calls finishing within +-10 ms of the (scaled 0.4 s) deadline, a third of them raising: exactly one outcome per call (never settled twice), worst caller overshoot < 80 ms, no 'exception never retrieved', every permit returned",
          sum(kinds.values()) == 300 and {"ok", "error", "deadline"} <= set(kinds) and gs.settled_twice == 0 and over < SLACK and not unretrieved and not gs.active, f"{kinds}, worst overshoot {over * 1000:.1f} ms, unretrieved {len(unretrieved)}, discarded {gs.late_discarded}")
    # D12: slow CLEANUP after cancel (3 s) is the cleanup's business: caller still back at the deadline, permit comes back when cleanup ends, flagged stuck during the cleanup
    g = Guard()
    async def slow_cleanup():
        try: await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(3.0); raise
    t0 = time.monotonic(); r = await g.call(slow_cleanup); t_back = time.monotonic() - t0
    await asyncio.sleep(0.3); stuck_mid, active_mid = len(g.stuck), len(g.active)
    while g.active and time.monotonic() - t0 < 10: await asyncio.sleep(0.05)
    t_perm = time.monotonic() - t0
    check("D12 (G214-09) cancellation CLEANUP that takes 3 s: caller back at 5.0 s (+80 ms max), permit held and task flagged stuck while it cleans up, permit returned only when the cleanup really ended (~8 s)",
          r[0] == "deadline" and DEADLINE - 0.01 <= t_back <= DEADLINE + SLACK and stuck_mid == 1 and active_mid == 1 and 7.9 <= t_perm <= 8.5 and not g.active and not g.stuck, f"caller {t_back:.3f} s, mid: stuck {stuck_mid} active {active_mid}, permit back at {t_perm:.2f} s")
    # D13: scaled stress - 400 hanging calls, cap 400, deadline 0.5 s: the overshoot of the CALLER does not grow with the amount of cleanup going on
    g = Guard(cap=400, deadline=0.5, grace=0.1)
    async def hang_stubborn():
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            try: await asyncio.sleep(end - time.monotonic())
            except asyncio.CancelledError: continue
    t0 = time.monotonic(); res = await asyncio.gather(*[g.call(hang_stubborn) for _ in range(400)])
    over = max(el for _, el in res) - 0.5; mx_back = time.monotonic() - t0
    await asyncio.sleep(0.2); n_st = len(g.stuck)
    while g.active and time.monotonic() - t0 < 5: await asyncio.sleep(0.05)
    check("D13 (G214-09, scaled time) 400 simultaneous uncooperative calls: every caller back within deadline + 80 ms regardless of the 400 cleanups still running, all 400 flagged stuck by timer, all permits recovered at the end",
          all(k == "deadline" for k, _ in res) and over < SLACK and n_st == 400 and not g.active, f"worst caller overshoot {over * 1000:.1f} ms, stuck {n_st}")
    # forced termination: closing the shared client aborts in-flight requests
    SRV.cfg.update(hs=0.0, resp=0.0, drip=1.0)
    c = httpx.AsyncClient(timeout=httpx.Timeout(connect=3, read=5, write=5, pool=5), verify=cctx)
    tsk = asyncio.create_task(c.get(URL)); await asyncio.sleep(1.2); t0 = time.monotonic(); await c.aclose()
    try: await asyncio.wait_for(tsk, 2.0); ended = "ok"
    except Exception as e: ended = type(e).__name__
    check("D8 forced termination of the whole client (the 'escalation' step): aclose() ends a request in flight within 2 s", ended != "TimeoutError" and tsk.done() and time.monotonic() - t0 < 2.0, f"{ended} after {time.monotonic() - t0:.2f} s")

asyncio.run(scenario_d())

# static: every Supabase helper of the app uses the ASYNC client and never a thread (so cancellation is possible)
src_app = (Path(__file__).resolve().parents[2] / "bot_v5_legacy.py")
if src_app.exists():
    tree = ast.parse(src_app.read_text(encoding="utf-8")); bad, seen = [], 0
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith(("sb_", "_sb_"))]:
        seen += 1
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = ast.unparse(n.func)
                if f in ("asyncio.to_thread", "httpx.Client", "requests.get", "requests.post", "requests.request") or f.endswith("run_in_executor"): bad.append((fn.name, f))
    check("D9 (G214-07 scope) every Supabase helper in bot_v5_legacy.py (sb_*) uses only the async client: no to_thread / sync httpx / requests, so a deadline CAN cancel and close the connection", seen >= 10 and not bad, f"{seen} helpers checked, offenders {bad}")
else:
    check("D9 bot_v5_legacy.py present for the static check", False, str(src_app))


# doc <-> code constants: the sentence and the implementation must not drift apart again (G214-09 was exactly that)
doc_text = DOC.read_text(encoding="utf-8")
need = {"deadline": f"{DEADLINE:.1f}초", "grace": f"GRACE({GRACE}초)", "slack": f"허용 {int(SLACK * 1000)} ms", "cap": f"동시성 상한 {CAP}"}
miss = [k for k, v in need.items() if v not in doc_text]
check("D14 (G214-09 sweep) the numbers the design text states for the deadline, grace, caller slack and concurrency cap are the ones the reference model uses", not miss, f"missing in doc: {miss}")

print(f"\n{sum(results)}/{len(results)} claims reproduced"); sys.exit(0 if all(results) else 1)
