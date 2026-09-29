# Progress + stop/start test used during development. Not needed in production.
"""Progress + stop/start test, driven through the admin page (Caddy HTTPS)."""
import glob, imaplib, re, subprocess, time
import pymysql, requests

BASE = "https://127.0.0.1:8443"
CA = glob.glob("/root/.local/share/caddy/pki/authorities/local/root.crt")[0]
R = []


def report(name, ok, detail=""):
    R.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def q(sql):
    c = pymysql.connect(host="localhost", user="rc", password="rc", database="roundcubemail", autocommit=True)
    with c.cursor() as cur:
        cur.execute(sql); rows = cur.fetchall()
    c.close(); return rows


def local_count(user="big2@t.com", pw="big2local"):
    b = imaplib.IMAP4("127.0.0.1", 143); b.login(user, pw)
    n = int(b.select("INBOX", readonly=True)[1][0]); b.logout(); return n


def pending():
    rows = q("SELECT pending FROM sync_progress WHERE account_email='big2@t.com' AND folder='INBOX'")
    return rows[0][0] if rows else None


def shot(path):
    subprocess.run(["python3", "-c", f"""
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(ignore_https_errors=True, viewport={{'width':1000,'height':900}})
    pg.goto('{BASE}/login'); pg.fill('#u','admin'); pg.fill('#p','Correct-Horse-9x'); pg.click('button')
    pg.wait_for_load_state(); pg.click('details summary') if pg.query_selector('details summary') else None
    pg.screenshot(path='{path}', full_page=True); b.close()
"""], timeout=90)


s = requests.Session(); s.verify = CA; s.trust_env = False
s.post(f"{BASE}/login", data={"username": "admin", "password": "Correct-Horse-9x"})
token = re.search(r'name="csrf" value="([^"]+)"', s.get(f"{BASE}/").text).group(1)

# 1. progress moves
t0 = time.time()
while (pending() is None or pending() > 3200) and time.time() - t0 < 120:
    time.sleep(1)
p1, l1 = pending(), local_count()
time.sleep(6)
p2, l2 = pending(), local_count()
report("progress is reported and moves (waiting count goes down, Roundcube count goes up)",
       p1 is not None and p2 < p1 and l2 > l1, f"(waiting {p1} -> {p2}, in Roundcube {l1} -> {l2})")
html = s.get(f"{BASE}/").text
report("admin page shows the download with bar, numbers and time left",
       "Waiting to download" in html and "big2@t.com" in html and "left</span>" in html and 'class="bar' in html)
shot("/tmp/progress_mid.png")

# 2. stop one account mid-download
s.post(f"{BASE}/toggle", data={"email": "big2@t.com", "csrf": token})
t_stop = time.time()
prev, stable_at = local_count(), None
while time.time() - t_stop < 90:
    time.sleep(2)
    now = local_count()
    if now == prev:
        stable_at = stable_at or time.time()
        if time.time() - stable_at >= 6:
            break
    else:
        prev, stable_at = now, None
took = (stable_at or time.time()) - t_stop
frozen = local_count()
time.sleep(12)
st = q("SELECT pull_state FROM sync_account_status WHERE account_email='big2@t.com'")[0][0]
report("'Stop sync' halts a running download quickly", took < 30 and local_count() == frozen and st == "stopped",
       f"(stopped after {took:.0f}s at {frozen} mails, status '{st}')")
report("admin page shows it as stopped", "Sync stopped for this account" in s.get(f"{BASE}/").text)

# 3. start again -> continues and finishes without duplicates
s.post(f"{BASE}/toggle", data={"email": "big2@t.com", "csrf": token})
t_start = time.time()
while pending() != 0 and time.time() - t_start < 400:
    time.sleep(3)
b = imaplib.IMAP4("127.0.0.1", 143); b.login("big2@t.com", "big2local"); b.select("INBOX", readonly=True)
t, d = b.fetch("1:*", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
ids = [x[1].strip() for x in d if isinstance(x, tuple)]
report("'Start sync' resumes where it stopped and finishes with no duplicates",
       len(ids) == 4000 and len(set(ids)) == 4000, f"(local {len(ids)}, unique {len(set(ids))}, "
       f"{time.time() - t_start:.0f}s to finish)")
time.sleep(4)
shot("/tmp/progress_done.png")

# 4. stop ALL sync, then start again
s.post(f"{BASE}/sync", data={"action": "stop", "csrf": token})
t_all = time.time()
ok = False
while time.time() - t_all < 60:
    states = dict(q("SELECT account_email, pull_state FROM sync_account_status"))
    if all(v == "stopped" for k, v in states.items() if k in ("big@t.com", "big2@t.com", "rajat@t.com")):
        ok = True; break
    time.sleep(2)
report("'Stop all sync' stops every account", ok, f"({time.time() - t_all:.0f}s)")
html = s.get(f"{BASE}/").text
report("page shows 'Sync stopped' banner and 'Start all sync' button", "Sync stopped" in html and "Start all sync" in html)
shot("/tmp/progress_stopped.png")
s.post(f"{BASE}/sync", data={"action": "start", "csrf": token})
t_all = time.time(); ok = False
while time.time() - t_all < 60:
    states = dict(q("SELECT account_email, pull_state FROM sync_account_status"))
    if states.get("big2@t.com") == "up to date" and states.get("big@t.com") == "up to date":
        ok = True; break
    time.sleep(2)
report("'Start all sync' brings every account back", ok, f"({time.time() - t_all:.0f}s)")

# 5. new mail still arrives after all that
a = imaplib.IMAP4_SSL("127.0.0.1", 10994, ssl_context=__import__("ssl")._create_unverified_context())
a.login("big2@t.com", "big2pw")
a.append("INBOX", None, None, b"From: x@y.com\r\nSubject: after restart\r\nMessage-ID: <after-restart@x>\r\n\r\nhi\r\n")
t = time.time()
while local_count() < 4001 and time.time() - t < 30:
    time.sleep(0.5)
report("new mail still arrives in about a second afterwards", local_count() == 4001, f"({time.time() - t:.1f}s)")
print(f"\n{sum(R)}/{len(R)} passed")
