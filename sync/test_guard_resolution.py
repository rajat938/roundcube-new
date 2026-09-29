# Test used during development. Not needed in production.
"""Guards must pause, then resolve (auto-confirm or Approve) -- never stay stuck.
Usage: claude_guard_test.py run1   (sync running with DELETE_LOCAL_ON_REMOTE_DELETE=0)
       claude_guard_test.py run2   (sync running with DELETE_LOCAL_ON_REMOTE_DELETE=1)
GUARD_CONFIRM_MINUTES=1 in both."""
import glob, imaplib, re, ssl, subprocess, sys, time
import pymysql, requests

R = []
MODE = sys.argv[1]


def report(name, ok, detail=""):
    R.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def q(sql, args=()):
    c = pymysql.connect(host="localhost", user="rc", password="rc", database="roundcubemail", autocommit=True)
    with c.cursor() as cur:
        cur.execute(sql, args); rows = cur.fetchall()
    c.close(); return rows


def retry_login(factory, u, p):
    for _ in range(12):
        try:
            c = factory(); c.login(u, p); return c
        except imaplib.IMAP4.error:
            time.sleep(1)
    raise RuntimeError("login " + u)


def H(u, p):
    return retry_login(lambda: imaplib.IMAP4_SSL("127.0.0.1", 10994, ssl_context=ssl._create_unverified_context()), u, p)


def L(u, p):
    return retry_login(lambda: imaplib.IMAP4("127.0.0.1", 143), u, p)


def ids(c, folder, mid=None):
    c.select(f'"{folder}"', readonly=True)
    t, d = c.uid("SEARCH", "HEADER", "MESSAGE-ID", f'"{mid}"') if mid else c.uid("SEARCH", "ALL")
    return [int(x) for x in d[0].split()] if d and d[0] else []


def delete(c, folder, uids):
    c.select(f'"{folder}"')
    assert c.uid("STORE", ",".join(map(str, uids)), "+FLAGS", "(\\Deleted)")[0] == "OK"
    c.expunge()


def wait_for(fn, timeout):
    t = time.time()
    while time.time() - t < timeout:
        if fn():
            return time.time() - t
        time.sleep(2)
    return None


def make_account(name, rpw, lpw):
    vm = subprocess.run(["id", "-u", "vmail"], capture_output=True, text=True).stdout.strip()
    vg = subprocess.run(["id", "-g", "vmail"], capture_output=True, text=True).stdout.strip()
    for side, pw in (("A", rpw), ("B", lpw)):
        f = f"/srv/e2e/{side}/users"
        if not any(l.startswith(name + ":") for l in open(f).read().splitlines()):
            open(f, "a").write(f"{name}:{{PLAIN}}{pw}:{vm}:{vg}::/srv/e2e/{side}/mail/{name.split('@')[0]}\n")
    h, l = H(name, rpw), L(name, lpw)
    for f in ("INBOX.Drafts", "INBOX.Trash", "INBOX.Sent"):
        h.create(f'"{f}"'); l.create(f'"{f}"')
    return h


def register(name, rpw, lpw):
    q("DELETE FROM mirror_accounts WHERE email=%s", (name,))
    q("INSERT INTO mirror_accounts (email, hostinger_password, local_password, imap_host, imap_port) "
      "VALUES (%s,%s,%s,'127.0.0.1',10993)", (name, rpw, lpw))


def state(name, svc="pull"):
    r = q(f"SELECT {svc}_state, {svc}_detail FROM sync_account_status WHERE account_email=%s", (name,))
    return r[0] if r else (None, None)


def draft(mid, text):
    return (f"From: a@t.com\r\nSubject: {text}\r\nDate: Thu, 24 Sep 2026 10:00:00 +0000\r\n"
            f"Message-ID: {mid}\r\n\r\n{text}\r\n").encode()


def admin():
    s = requests.Session(); s.verify = glob.glob("/root/.local/share/caddy/pki/authorities/local/root.crt")[0]
    s.trust_env = False
    s.post("https://127.0.0.1:8443/login", data={"username": "admin", "password": "Correct-Horse-9x"})
    html = s.get("https://127.0.0.1:8443/").text
    return s, re.search(r'name="csrf" value="([^"]+)"', html).group(1), html


D, T = "INBOX.Drafts", "INBOX.Trash"

if MODE == "run1":
    # ---- 1. bulk delete on Hostinger, DELETE_LOCAL=0: no stuck "Problem" ----
    h = make_account("k1@t.com", "k1pw", "k1local")
    for i in range(40):
        h.append("INBOX", None, None, f"From: x@y\r\nSubject: m{i}\r\nMessage-ID: <k1m{i}@x>\r\n\r\nhi\r\n".encode())
    register("k1@t.com", "k1pw", "k1local")
    wait_for(lambda: len(ids(L("k1@t.com", "k1local"), "INBOX")) == 40, 120)
    time.sleep(8)
    h = H("k1@t.com", "k1pw"); delete(h, "INBOX", ids(h, "INBOX")[:25])
    states = set()
    t0 = time.time()
    while time.time() - t0 < 110:
        states.add(state("k1@t.com")[0]); time.sleep(3)
    maprows = q("SELECT COUNT(*) FROM sync_msg_map WHERE account_email='k1@t.com' AND folder='INBOX'")[0][0]
    holds = q("SELECT COUNT(*) FROM sync_guard WHERE account_email='k1@t.com'")[0][0]
    report("DELETE_LOCAL=0: 25 deleted on Hostinger -> never shown as a Problem",
           "error" not in states, f"(states seen: {sorted(s for s in states if s)})")
    report("...bookkeeping catches up by itself, nothing stays held, local mail untouched",
           maprows == 15 and holds == 0 and len(ids(L("k1@t.com", "k1local"), "INBOX")) == 40,
           f"(map rows {maprows}, holds {holds}, local {len(ids(L('k1@t.com', 'k1local'), 'INBOX'))})")

    # ---- 2. 5 drafts deleted in Roundcube at once: held -> Approve -> applied ----
    make_account("k2@t.com", "k2pw", "k2local")
    register("k2@t.com", "k2pw", "k2local")
    time.sleep(12)
    l = L("k2@t.com", "k2local")
    for i in range(5):
        l.append(f'"{D}"', "(\\Draft \\Seen)", None, draft(f"<k2d{i}@x>", f"draft {i}"))
    wait_for(lambda: len(ids(H("k2@t.com", "k2pw"), D)) == 5, 60)
    time.sleep(5)
    l = L("k2@t.com", "k2local"); delete(l, D, ids(l, D))
    held = wait_for(lambda: q("SELECT COUNT(*) FROM sync_guard WHERE account_email='k2@t.com' AND guard='push-drafts'")[0][0] == 1, 30)
    report("5 drafts deleted in Roundcube at once -> held, Hostinger untouched",
           held is not None and len(ids(H("k2@t.com", "k2pw"), D)) == 5)
    # sync keeps working meanwhile
    L("k2@t.com", "k2local").append(f'"{D}"', "(\\Draft \\Seen)", None, draft("<k2new@x>", "new while held"))
    up = wait_for(lambda: ids(H("k2@t.com", "k2pw"), D, "<k2new@x>"), 30)
    report("...while held, a new draft still reaches Hostinger", up is not None, f"({up:.0f}s)" if up else "")
    s, tok, html = admin()
    report("...admin page shows it with an Approve button", "Waiting for you:" in html and "Approve</button>" in html)
    s.post("https://127.0.0.1:8443/approve", data={"email": "k2@t.com", "guard": "push-drafts", "csrf": tok})
    done = wait_for(lambda: len(ids(H("k2@t.com", "k2pw"), D)) == 1 and len(ids(H("k2@t.com", "k2pw"), T)) == 5, 30)
    time.sleep(8)
    report("...after Approve: the 5 drafts go to Hostinger's Trash, the new one stays",
           done is not None, f"({done:.0f}s)" if done else f"(Drafts {len(ids(H('k2@t.com', 'k2pw'), D))}, Trash {len(ids(H('k2@t.com', 'k2pw'), T))})")
    report("...and the status is back to normal", state("k2@t.com", "push")[0] == "up to date"
           and q("SELECT COUNT(*) FROM sync_guard WHERE account_email='k2@t.com'")[0][0] == 0,
           f"({state('k2@t.com', 'push')[0]})")

    # ---- 3. phone deletes 5 of 6 drafts: held -> confirms by itself ----
    h = make_account("k3@t.com", "k3pw", "k3local")
    for i in range(6):
        h.append(f'"{D}"', "(\\Draft)", None, draft(f"<k3d{i}@x>", f"phone draft {i}"))
    register("k3@t.com", "k3pw", "k3local")
    wait_for(lambda: len(ids(L("k3@t.com", "k3local"), D)) == 6, 120)
    time.sleep(8)
    h = H("k3@t.com", "k3pw"); delete(h, D, ids(h, D)[:5])
    held = wait_for(lambda: state("k3@t.com")[0] == "error", 45)
    report("phone deletes 5 of 6 drafts -> held (Roundcube copies kept for now)",
           held is not None and len(ids(L("k3@t.com", "k3local"), D)) == 6)
    done = wait_for(lambda: len(ids(L("k3@t.com", "k3local"), D)) == 1, 150)
    time.sleep(5)
    report("...confirms by itself after the wait: copies moved to Roundcube's Trash, status normal",
           done is not None and len(ids(L("k3@t.com", "k3local"), T)) == 5 and state("k3@t.com")[0] == "up to date",
           f"(after {held + done:.0f}s; Trash {len(ids(L('k3@t.com', 'k3local'), T))}, status {state('k3@t.com')[0]})"
           if done else f"(local drafts {len(ids(L('k3@t.com', 'k3local'), D))}, status {state('k3@t.com')})")

    # ---- 4. phone deletes both of 2 drafts: small -> applied at once, no Problem ----
    h = make_account("k4@t.com", "k4pw", "k4local")
    for i in range(2):
        h.append(f'"{D}"', "(\\Draft)", None, draft(f"<k4d{i}@x>", f"d{i}"))
    register("k4@t.com", "k4pw", "k4local")
    wait_for(lambda: len(ids(L("k4@t.com", "k4local"), D)) == 2, 120)
    time.sleep(8)
    h = H("k4@t.com", "k4pw"); delete(h, D, ids(h, D))
    states = set(); t0 = time.time()
    done = None
    while time.time() - t0 < 50:
        states.add(state("k4@t.com")[0])
        if not ids(L("k4@t.com", "k4local"), D):
            done = time.time() - t0; break
        time.sleep(2)
    report("phone deletes both of 2 drafts -> applied right away, no Problem",
           done is not None and "error" not in states, f"({done:.0f}s)" if done else "")

else:
    # ---- 5. DELETE_LOCAL=1: bulk delete -> shown, then confirms by itself ----
    h = make_account("k5@t.com", "k5pw", "k5local")
    for i in range(40):
        h.append("INBOX", None, None, f"From: x@y\r\nSubject: m{i}\r\nMessage-ID: <k5m{i}@x>\r\n\r\nhi\r\n".encode())
    register("k5@t.com", "k5pw", "k5local")
    wait_for(lambda: len(ids(L("k5@t.com", "k5local"), "INBOX")) == 40, 120)
    time.sleep(8)
    h = H("k5@t.com", "k5pw"); delete(h, "INBOX", ids(h, "INBOX")[:25])
    held = wait_for(lambda: state("k5@t.com")[0] == "error", 45)
    report("DELETE_LOCAL=1: 25 deleted on Hostinger -> held and shown", held is not None
           and len(ids(L("k5@t.com", "k5local"), "INBOX")) == 40, f"({state('k5@t.com')[1][:70] if held else ''})")
    done = wait_for(lambda: len(ids(L("k5@t.com", "k5local"), "INBOX")) == 15, 150)
    time.sleep(5)
    report("...confirms by itself: 25 local copies moved to Roundcube's Trash, status normal",
           done is not None and len(ids(L("k5@t.com", "k5local"), T)) == 25 and state("k5@t.com")[0] == "up to date",
           f"(Trash {len(ids(L('k5@t.com', 'k5local'), T))}, status {state('k5@t.com')[0]})")

    # ---- 6. same, but Approve right away ----
    h = H("k5@t.com", "k5pw")
    for i in range(30):
        h.append("INBOX", None, None, f"From: x@y\r\nSubject: n{i}\r\nMessage-ID: <k5n{i}@x>\r\n\r\nhi\r\n".encode())
    wait_for(lambda: len(ids(L("k5@t.com", "k5local"), "INBOX")) == 45, 60)
    time.sleep(8)
    h = H("k5@t.com", "k5pw"); delete(h, "INBOX", [u for u in ids(h, "INBOX")][-25:])
    held = wait_for(lambda: q("SELECT COUNT(*) FROM sync_guard WHERE account_email='k5@t.com'")[0][0] == 1, 45)
    s, tok, html = admin()
    s.post("https://127.0.0.1:8443/approve", data={"email": "k5@t.com", "guard": "del:INBOX", "csrf": tok})
    done = wait_for(lambda: len(ids(L("k5@t.com", "k5local"), "INBOX")) == 20, 50)
    time.sleep(3)
    report("...or press Approve: applied at the next check, status back to normal",
           held is not None and done is not None and state("k5@t.com")[0] == "up to date",
           f"({done:.0f}s after Approve)" if done else f"(local {len(ids(L('k5@t.com', 'k5local'), 'INBOX'))})")

print(f"\n{sum(R)}/{len(R)} passed")
