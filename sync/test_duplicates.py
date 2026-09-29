# Test used during development. Not needed in production.
"""Duplicate Message-ID handling: every Hostinger copy must reach Roundcube."""
import imaplib, os, ssl, subprocess, time
import pymysql

U, G = os.getuid(), os.getgid()
R = []


def report(name, ok, detail=""):
    R.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def db():
    return pymysql.connect(host="localhost", user="rc", password="rc", database="roundcubemail", autocommit=True).cursor()


def H(user, pw):
    c = imaplib.IMAP4_SSL("127.0.0.1", 10994, ssl_context=ssl._create_unverified_context()); c.login(user, pw); return c


def L(user, pw):
    for attempt in range(10):          # test Dovecot re-reads its user file lazily
        try:
            c = imaplib.IMAP4("127.0.0.1", 143); c.login(user, pw); return c
        except imaplib.IMAP4.error:
            time.sleep(1)
    raise RuntimeError("local login failed")


def count(c, folder="INBOX"):
    return int(c.select(f'"{folder}"', readonly=True)[1][0])


def subjects(c, folder="INBOX"):
    c.select(f'"{folder}"', readonly=True)
    t, d = c.fetch("1:*", "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    return sorted(x[1].decode().strip() for x in d if isinstance(x, tuple))


def msg(mid, subject, body="hello"):
    return (f"From: a@ex.com\r\nDate: Thu, 24 Sep 2026 10:00:00 +0000\r\nSubject: {subject}\r\n"
            f"Message-ID: {mid}\r\n\r\n{body}\r\n").encode()


def add_user(name, rpw, lpw):
    vm = subprocess.run(["id", "-u", "vmail"], capture_output=True, text=True).stdout.strip()
    vg = subprocess.run(["id", "-g", "vmail"], capture_output=True, text=True).stdout.strip()
    for side, pw in (("A", rpw), ("B", lpw)):
        f = f"/srv/e2e/{side}/users"
        if name not in open(f).read():
            open(f, "a").write(f"{name}:{{PLAIN}}{pw}:{vm}:{vg}::/srv/e2e/{side}/mail/{name.split('@')[0]}\n")


def wait_for(fn, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        if fn():
            return time.time() - t
        time.sleep(1)
    return None


import sys
ONLY_MIG = "mig" in sys.argv
c = db()
if ONLY_MIG:
    c.execute("UPDATE mirror_accounts SET active=0 WHERE email='dup@t.com'")
# ---- 1. fresh account: duplicates + different mails sharing an ID ----
if not ONLY_MIG:
  add_user("dup@t.com", "duppw", "duplocal")
  h = H("dup@t.com", "duppw")
  for i in range(10):
      h.append("INBOX", None, None, msg(f"<u{i}@x>", f"unique {i}"))
  for i in range(3):                                   # exact duplicates
      raw = msg(f"<same{i}@x>", f"exact duplicate {i}")
      h.append("INBOX", None, None, raw); h.append("INBOX", None, None, raw)
  h.append("INBOX", None, None, msg("<shared@x>", "DIFFERENT mail A", "text A"))   # different mails, same ID
  h.append("INBOX", None, None, msg("<shared@x>", "DIFFERENT mail B", "text B"))
  c.execute("DELETE FROM mirror_accounts WHERE email='dup@t.com'")
  c.execute("INSERT INTO mirror_accounts (email, hostinger_password, local_password, imap_host, imap_port) "
            "VALUES ('dup@t.com','duppw','duplocal','127.0.0.1',10993)")
  remote_n = count(h)
  took = wait_for(lambda: count(L("dup@t.com", "duplocal")) >= remote_n, 120)
  time.sleep(5)
  l = L("dup@t.com", "duplocal")
  report("every Hostinger copy downloaded (duplicates kept)", count(l) == remote_n,
         f"(Hostinger {remote_n}, Roundcube {count(l)})")
  report("two DIFFERENT mails sharing one Message-ID: both downloaded",
         "Subject: DIFFERENT mail A" in subjects(l) and "Subject: DIFFERENT mail B" in subjects(l))

  # ---- 2. duplicate arriving as NEW mail ----
  h = H("dup@t.com", "duppw")
  h.append("INBOX", None, None, msg("<u0@x>", "unique 0"))       # same mail arrives again
  lat = wait_for(lambda: count(L("dup@t.com", "duplocal")) == remote_n + 1, 60)
  report("a duplicate arriving as new mail is downloaded too", lat is not None,
         f"({lat:.1f}s)" if lat is not None else f"(Roundcube {count(L('dup@t.com', 'duplocal'))})")

# ---- 3. migration: mirror already holds ONE copy of each duplicate pair ----
add_user("mig@t.com", "migpw", "miglocal")
h, l = H("mig@t.com", "migpw"), L("mig@t.com", "miglocal")
for i in range(4):
    raw = msg(f"<m{i}@x>", f"pair {i}")
    h.append("INBOX", None, None, raw); h.append("INBOX", None, None, raw)
    l.append("INBOX", None, None, raw)                                  # old dedupe left one copy
for i in range(20):
    raw = msg(f"<s{i}@x>", f"single {i}")
    h.append("INBOX", None, None, raw); l.append("INBOX", None, None, raw)
before = count(l)
c.execute("DELETE FROM mirror_accounts WHERE email='mig@t.com'")
c.execute("INSERT INTO mirror_accounts (email, hostinger_password, local_password, imap_host, imap_port) "
          "VALUES ('mig@t.com','migpw','miglocal','127.0.0.1',10993)")
wait_for(lambda: count(L("mig@t.com", "miglocal")) >= 28, 120)
time.sleep(8)
after = count(L("mig@t.com", "miglocal"))
report("existing mirror: only the missing copies are downloaded, nothing twice",
       before == 24 and after == 28, f"(Roundcube {before} -> {after}, Hostinger 28)")

# ---- 4. Hostinger deletes ONE of two copies (DELETE_LOCAL_ON_REMOTE_DELETE=1) ----
h = H("mig@t.com", "migpw"); h.select("INBOX")
t, d = h.uid("SEARCH", "HEADER", "MESSAGE-ID", '"<m0@x>"'); first = d[0].split()[0]
h.uid("STORE", first, "+FLAGS", "(\\Deleted)"); h.expunge()
ok = wait_for(lambda: count(L("mig@t.com", "miglocal")) == 27, 60)
time.sleep(25)
l = L("mig@t.com", "miglocal"); l.select("INBOX", readonly=True)
t, d = l.uid("SEARCH", "HEADER", "MESSAGE-ID", '"<m0@x>"')
left = len(d[0].split()) if d and d[0] else 0
report("Hostinger deletes one of two copies -> exactly one local copy removed, the other kept",
       ok is not None and left == 1 and count(l) == 27, f"(local copies of that mail: {left}, total {count(l)})")

print(f"\n{sum(R)}/{len(R)} passed")
