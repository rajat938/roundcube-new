# Test used during development. Not needed in production.
"""Tests for the review fixes (run with sync-pull/push active,
DELETE_LOCAL_ON_REMOTE_DELETE=1, short check intervals)."""
import imaplib, ssl, subprocess, sys, time
import pymysql

R = []


def report(name, ok, detail=""):
    R.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def db():
    return pymysql.connect(host="localhost", user="rc", password="rc", database="roundcubemail", autocommit=True).cursor()


def H(u, p):
    for _ in range(10):          # test Dovecot re-reads its user file lazily
        try:
            c = imaplib.IMAP4_SSL("127.0.0.1", 10994, ssl_context=ssl._create_unverified_context())
            c.login(u, p); return c
        except imaplib.IMAP4.error:
            time.sleep(1)
    raise RuntimeError("login")


def L(u, p):
    for _ in range(10):
        try:
            c = imaplib.IMAP4("127.0.0.1", 143); c.login(u, p); return c
        except imaplib.IMAP4.error:
            time.sleep(1)
    raise RuntimeError("login")


def add_user(name, rpw, lpw):
    vm = subprocess.run(["id", "-u", "vmail"], capture_output=True, text=True).stdout.strip()
    vg = subprocess.run(["id", "-g", "vmail"], capture_output=True, text=True).stdout.strip()
    for side, pw in (("A", rpw), ("B", lpw)):
        f = f"/srv/e2e/{side}/users"
        if not any(line.startswith(name + ":") for line in open(f).read().splitlines()):
            open(f, "a").write(f"{name}:{{PLAIN}}{pw}:{vm}:{vg}::/srv/e2e/{side}/mail/{name.split('@')[0]}\n")
    time.sleep(1.5)


def register(name, rpw, lpw):
    c = db(); c.execute("DELETE FROM mirror_accounts WHERE email=%s", (name,))
    c.execute("INSERT INTO mirror_accounts (email, hostinger_password, local_password, imap_host, imap_port) "
              "VALUES (%s,%s,%s,'127.0.0.1',10993)", (name, rpw, lpw))


def ids(conn, folder, mid=None):
    conn.select(f'"{folder}"', readonly=True)
    t, d = conn.uid("SEARCH", "HEADER", "MESSAGE-ID", f'"{mid}"') if mid else conn.uid("SEARCH", "ALL")
    return [int(x) for x in d[0].split()] if d and d[0] else []


def wait_for(fn, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        if fn():
            return time.time() - t
        time.sleep(1)
    return None


def draft(mid, text):
    return (f"From: g@t.com\r\nSubject: draft {text}\r\nDate: Thu, 24 Sep 2026 10:00:00 +0000\r\n"
            f"Message-ID: {mid}\r\n\r\n{text}\r\n").encode()


D, T, S = "INBOX.Drafts", "INBOX.Trash", "INBOX.Sent"

# ---------- account g1: flags + drafts mass-delete guard ----------
add_user("g1@t.com", "g1pw", "g1local")
for c in (H("g1@t.com", "g1pw"), L("g1@t.com", "g1local")):
    for f in (D, T, S):
        c.create(f'"{f}"')
register("g1@t.com", "g1pw", "g1local")
time.sleep(15)

# 1. \Deleted never travels to Hostinger
l = L("g1@t.com", "g1local")
l.append(f'"{S}"', "(\\Seen \\Deleted)", None,
         b"From: g1@t.com\r\nSubject: sent flagged deleted\r\nMessage-ID: <fl1@x>\r\n\r\nhi\r\n")
got = wait_for(lambda: ids(H("g1@t.com", "g1pw"), S, "<fl1@x>"), 30)
h = H("g1@t.com", "g1pw"); u = ids(h, S, "<fl1@x>")
flags = h.uid("FETCH", str(u[0]), "(FLAGS)")[1][0].decode() if u else ""
report("mail uploaded to Hostinger never carries \\Deleted", got is not None and "\\Deleted" not in flags
       and "\\Seen" in flags, f"(Hostinger flags: {flags[flags.find('FLAGS'):]})")

# 2. five drafts, then the local Drafts folder "comes up empty"
l = L("g1@t.com", "g1local")
for i in range(5):
    l.append(f'"{D}"', "(\\Draft \\Seen)", None, draft(f"<gd{i}@x>", f"v{i}"))
wait_for(lambda: len(ids(H("g1@t.com", "g1pw"), D)) == 5, 60)
time.sleep(5)
trash_before = len(ids(H("g1@t.com", "g1pw"), T))
l = L("g1@t.com", "g1local"); l.select(f'"{D}"')
l.uid("STORE", "1:*", "+FLAGS", "(\\Deleted)"); l.expunge()            # local drafts all gone
time.sleep(25)
h = H("g1@t.com", "g1pw")
report("local Drafts suddenly empty -> Hostinger drafts NOT touched",
       len(ids(h, D)) == 5 and len(ids(h, T)) == trash_before, f"(Hostinger Drafts {len(ids(h, D))}, Trash {len(ids(h, T))})")
time.sleep(35)   # must STAY visible, not be overwritten by the next cycle
st = db(); st.execute("SELECT push_state, push_detail FROM sync_account_status WHERE account_email='g1@t.com'")
row = st.fetchone()
report("the admin page keeps showing it as a problem", row and row[0] == "error", f"({row[1][:60] if row else row}...)")

# ---------- account g2: Hostinger-side mass/single deletes ----------
add_user("g2@t.com", "g2pw", "g2local")
h = H("g2@t.com", "g2pw")
for f in (D, T, S):
    h.create(f'"{f}"'); L("g2@t.com", "g2local").create(f'"{f}"')
for i in range(40):
    h.append("INBOX", None, None, f"From: x@y\r\nSubject: m{i}\r\nMessage-ID: <g2m{i}@x>\r\n\r\nhi\r\n".encode())
for i in range(5):
    h.append(f'"{D}"', "(\\Draft)", None, draft(f"<g2d{i}@x>", f"d{i}"))
register("g2@t.com", "g2pw", "g2local")
wait_for(lambda: len(ids(L("g2@t.com", "g2local"), "INBOX")) == 40 and len(ids(L("g2@t.com", "g2local"), D)) == 5, 120)
time.sleep(10)

# 3. Hostinger loses 3 of 5 drafts at once -> local drafts kept
h = H("g2@t.com", "g2pw"); victims = ids(h, D)[:4]; h.select(f'"{D}"')
assert h.uid("STORE", ",".join(map(str, victims)), "+FLAGS", "(\\Deleted)")[0] == "OK"; h.expunge()
assert len(ids(H("g2@t.com", "g2pw"), D)) == 1, "test setup: Hostinger drafts not deleted"
time.sleep(40)
report("4 of 5 drafts vanish on Hostinger at once -> local drafts kept (held)",
       len(ids(L("g2@t.com", "g2local"), D)) == 5, f"(local drafts {len(ids(L('g2@t.com', 'g2local'), D))})")

# 4. Hostinger loses 25 INBOX mails at once -> nothing removed locally
h = H("g2@t.com", "g2pw"); victims = ids(h, "INBOX")[:25]; h.select("INBOX")
assert h.uid("STORE", ",".join(map(str, victims)), "+FLAGS", "(\\Deleted)")[0] == "OK"; h.expunge()
assert len(ids(H("g2@t.com", "g2pw"), "INBOX")) == 15, "test setup: Hostinger mails not deleted"
time.sleep(40)
n_local = len(ids(L("g2@t.com", "g2local"), "INBOX"))
report("25 INBOX mails vanish on Hostinger at once -> local mirror untouched", n_local == 40, f"(local {n_local})")

time.sleep(5)
st = db(); st.execute("SELECT pull_state, pull_detail FROM sync_account_status WHERE account_email='g2@t.com'")
row = st.fetchone()
report("admin page shows the Hostinger mass-delete as a problem", row and row[0] == "error"
       and "disappeared from Hostinger" in (row[1] or ""), f"({(row[1] or '')[:70] if row else row})")

# ---------- account g3: normal single deletes on Hostinger + IDLE speed ----------
add_user("g3@t.com", "g3pw", "g3local")
h = H("g3@t.com", "g3pw")
for f in (D, T, S):
    h.create(f'"{f}"'); L("g3@t.com", "g3local").create(f'"{f}"')
for i in range(10):
    h.append("INBOX", None, None, f"From: x@y\r\nSubject: m{i}\r\nMessage-ID: <g3m{i}@x>\r\n\r\nhi\r\n".encode())
for i in range(3):
    h.append(f'"{D}"', "(\\Draft)", None, draft(f"<g3d{i}@x>", f"d{i}"))
register("g3@t.com", "g3pw", "g3local")
wait_for(lambda: len(ids(L("g3@t.com", "g3local"), "INBOX")) == 10 and len(ids(L("g3@t.com", "g3local"), D)) == 3, 120)
time.sleep(25)   # let the IDLE watcher settle
h = H("g3@t.com", "g3pw")
h.append("INBOX", None, None, b"From: x@y\r\nSubject: idle test\r\nMessage-ID: <g3idle@x>\r\n\r\nhi\r\n")
lat = wait_for(lambda: ids(L("g3@t.com", "g3local"), "INBOX", "<g3idle@x>"), 40)
report("IDLE still works through the guard (new mail in seconds)", lat is not None and lat < 8,
       f"({lat:.1f}s)" if lat is not None else "")
h = H("g3@t.com", "g3pw"); h.select("INBOX")
u = ids(h, "INBOX", "<g3m0@x>")[0]; h.select("INBOX"); assert h.uid("STORE", str(u), "+FLAGS", "(\\Deleted)")[0] == "OK"; h.expunge()
u = ids(h, D, "<g3d0@x>")[0]; h.select(f'"{D}"'); assert h.uid("STORE", str(u), "+FLAGS", "(\\Deleted)")[0] == "OK"; h.expunge()
ok = wait_for(lambda: not ids(L("g3@t.com", "g3local"), "INBOX", "<g3m0@x>")
              and not ids(L("g3@t.com", "g3local"), D, "<g3d0@x>"), 90)
l = L("g3@t.com", "g3local")
in_trash = bool(ids(l, T, "<g3m0@x>")) and bool(ids(l, T, "<g3d0@x>"))
report("single delete on Hostinger -> local copy moved to Roundcube's Trash (recoverable)",
       ok is not None and in_trash, f"(INBOX {len(ids(l, 'INBOX'))}, Drafts {len(ids(l, D))}, "
       f"both in local Trash: {in_trash})")

# ---------- IDLE connection guard ----------
sys.path.insert(0, "/home/claude/sync")
import os
os.environ.update(DB_HOST="x", DB_USER="x", DB_PASSWORD="x", DB_NAME="x")
import common
from imapclient import IMAPClient
cl = IMAPClient("127.0.0.1", port=10994, ssl=True, ssl_context=ssl._create_unverified_context())
common.lock_down_idle_client(cl)
cl.login("g2@t.com", "g2pw"); cl.select_folder("INBOX", readonly=True); cl.idle(); cl.idle_check(timeout=1); cl.idle_done()
blocked = []
for name, fn in [("read-write SELECT", lambda: cl.select_folder("INBOX")),
                 ("EXPUNGE", lambda: cl.expunge()), ("DELETE folder", lambda: cl.delete_folder("INBOX.Trash")),
                 ("STORE \\Deleted", lambda: cl.delete_messages([1])), ("CREATE", lambda: cl.create_folder("X"))]:
    try:
        fn(); blocked.append((name, False))
    except common.ForbiddenCommand:
        blocked.append((name, True))
    except Exception as e:
        blocked.append((name, isinstance(e.__context__, common.ForbiddenCommand)))
report("IDLE connection: login/EXAMINE/IDLE work, everything else blocked", all(b for _n, b in blocked),
       ", ".join(n for n, b in blocked if not b))

print(f"\n{sum(R)}/{len(R)} passed")
