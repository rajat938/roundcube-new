# Test used during development. Not needed in production.
"""End-to-end test of sync-pull + sync-push against a fake Hostinger
(Dovecot, TLS, 80 ms RTT via proxy) and a local Dovecot mirror."""
import email.utils, imaplib, os, random, ssl, subprocess, sys, time
import pymysql

SRC = "/home/claude/sync"
ENV = dict(os.environ, DB_HOST="localhost", DB_USER="rc", DB_PASSWORD="rc", DB_NAME="roundcubemail",
           LOCAL_IMAP_HOST="dovecot", LOCAL_IMAP_PORT="143", REMOTE_TLS_VERIFY="0",
           ACCOUNT_RELOAD_INTERVAL="5", PYTHONUNBUFFERED="1")
ENV.update({k: v for k, v in (a.split("=", 1) for a in sys.argv[1:])})
ACC = "rajat@t.com"
results = []


def db():
    return pymysql.connect(host="localhost", user="rc", password="rc", database="roundcubemail", autocommit=True)


def H():   # direct connection to fake Hostinger (test harness only, no latency)
    c = imaplib.IMAP4_SSL("127.0.0.1", 10994, ssl_context=ssl._create_unverified_context()); c.login(ACC, "hpw"); return c


def L():
    c = imaplib.IMAP4("127.0.0.1", 143); c.login(ACC, "lpw"); return c


def find(conn, folder, mid):
    conn.select(f'"{folder}"', readonly=True)
    t, d = conn.uid("SEARCH", "HEADER", "MESSAGE-ID", f'"{mid}"')
    return [int(x) for x in d[0].split()] if d and d[0] else []


def flags(conn, folder, uid):
    conn.select(f'"{folder}"', readonly=True)
    t, d = conn.uid("FETCH", str(uid), "(FLAGS)")
    return imaplib.ParseFlags(d[0])


def count(conn, folder):
    t, d = conn.select(f'"{folder}"', readonly=True); return int(d[0])


def local_unique():
    l = L(); l.select("INBOX", readonly=True)
    t, d = l.fetch("1:*", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    return len({x[1].strip() for x in d if isinstance(x, tuple)})


def _with(conn, uids, fn):
    with conn.permit_draft_cleanup(uids):
        return fn()


def wait(pred, timeout, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time() - t0
        time.sleep(step)
    return None


def report(name, ok, detail=""):
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def new_mail(folder="INBOX", subject="hi"):
    mid = f"<t{random.randint(0, 10**12)}@test>"
    h = H()
    h.append(f'"{folder}"', None, imaplib.Time2Internaldate(time.time()),
             f"From: boss@ex.com\r\nTo: {ACC}\r\nDate: {email.utils.formatdate()}\r\nSubject: {subject}\r\n"
             f"Message-ID: {mid}\r\n\r\nbody\r\n".encode())
    h.logout(); return mid


# ---------------------------------------------------------------- setup
c = db().cursor()
for stmt in ["DROP TABLE IF EXISTS sync_msg_map", "DROP TABLE IF EXISTS sync_folder_state",
             "DROP TABLE IF EXISTS sent_upload_state", "DROP TABLE IF EXISTS mirror_accounts"]:
    c.execute(stmt)
subprocess.run([sys.executable, "-c", "import common; common.ensure_schema()"], cwd=SRC, env=ENV, check=True,
               capture_output=True)
c.execute("INSERT INTO mirror_accounts (email, hostinger_password, local_password, imap_host, imap_port) "
          "VALUES (%s,'hpw','lpw','127.0.0.1',10993)", (ACC,))

h0, l0 = count(H(), "INBOX"), count(L(), "INBOX")
logs = {n: open(f"/srv/e2e/{n}.log", "w") for n in ("pull", "push")}
t_start = time.time()
procs = [subprocess.Popen([sys.executable, f"{SRC}/{n}.py"], cwd=SRC, env=ENV, stdout=logs[n],
                          stderr=subprocess.STDOUT) for n in ("pull", "push")]
try:
    # 1. migration: existing mirror is matched, not duplicated
    took = wait(lambda: db().cursor().execute(
        "SELECT 1 FROM sync_msg_map WHERE account_email=%s AND folder='INBOX'", (ACC,)) >= h0, 600, 1)
    l1 = count(L(), "INBOX")
    report("migration: existing 5,700 mails matched, no duplicates",
           took is not None and l1 == h0 and local_unique() == l1, f"(first pass {took:.0f}s, Hostinger {h0}, local {l0}->{l1}, unique Message-IDs {local_unique()})" if took else "")
    time.sleep(3)

    # 2. new-mail latency
    lats = []
    for i in range(5):
        time.sleep(random.uniform(3, 8))
        mid = new_mail(subject=f"latency {i}")
        lat = wait(lambda: find(L(), "INBOX", mid), 120)
        lats.append(lat if lat is not None else 999)
    report("new mail reaches local INBOX fast", max(lats) < 10,
           "(" + ", ".join(f"{x:.1f}s" for x in lats) + ")")

    # 3. read in Roundcube -> read on Hostinger
    mid = new_mail(subject="read me locally"); wait(lambda: find(L(), "INBOX", mid), 60)
    l = L(); luid = find(l, "INBOX", mid)[0]; l.select("INBOX"); l.uid("STORE", str(luid), "+FLAGS", "(\\Seen)")
    h = H(); ruid = find(h, "INBOX", mid)[0]
    lat = wait(lambda: "\\Seen" in [f.decode() for f in flags(H(), "INBOX", ruid)], 30)
    report("read locally -> read on Hostinger", lat is not None, f"({lat:.1f}s)" if lat is not None else "")

    # 4. read on phone (Hostinger) -> read locally, and NOT reverted
    mid = new_mail(subject="read me on phone"); wait(lambda: find(L(), "INBOX", mid), 60)
    time.sleep(2)
    h = H(); ruid = find(h, "INBOX", mid)[0]; h.select("INBOX"); h.uid("STORE", str(ruid), "+FLAGS", "(\\Seen)")
    luid = find(L(), "INBOX", mid)[0]
    lat = wait(lambda: "\\Seen" in [f.decode() for f in flags(L(), "INBOX", luid)], 60)
    time.sleep(10)
    still = "\\Seen" in [f.decode() for f in flags(H(), "INBOX", ruid)]
    report("read on phone -> read locally, not reverted on Hostinger", lat is not None and still,
           f"(local after {lat:.1f}s; Hostinger still read after 10s: {still})" if lat is not None else "")

    # 5. delete + move in Roundcube: stays gone locally, Hostinger untouched
    mid_del = new_mail(subject="delete me"); mid_mv = new_mail(subject="move me")
    wait(lambda: find(L(), "INBOX", mid_del) and find(L(), "INBOX", mid_mv), 60)
    time.sleep(2)
    l = L(); l.select("INBOX")
    u_del = find(l, "INBOX", mid_del)[0]; u_mv = find(l, "INBOX", mid_mv)[0]; l.select("INBOX")
    l.uid("MOVE", str(u_del), "INBOX.Trash")                 # what Roundcube's Delete does
    l.uid("MOVE", str(u_mv), "INBOX.Junk")                   # a local move
    l.select("INBOX.Trash"); tu = find(l, "INBOX.Trash", mid_del)[0]; l.select("INBOX.Trash")
    l.uid("STORE", str(tu), "+FLAGS", "(\\Deleted)"); l.expunge()   # "Empty Trash"
    new_mail(subject="trigger a sync")                         # force an INBOX pull
    time.sleep(40)                                             # > INBOX_CHECK + other folders cycle
    back = bool(find(L(), "INBOX", mid_del)) or bool(find(L(), "INBOX", mid_mv))
    h = H()
    on_h = bool(find(h, "INBOX", mid_del)) and bool(find(h, "INBOX", mid_mv))
    in_h_junk_trash = bool(find(h, "INBOX.Junk", mid_mv)) or bool(find(h, "INBOX.Trash", mid_del))
    report("local delete/move/empty-trash: not re-pulled locally", not back)
    report("local delete/move/empty-trash: Hostinger unchanged (still in INBOX, nothing moved)",
           on_h and not in_h_junk_trash)

    # 6. sent mail: uploaded once, no local duplicate
    smid = f"<sent{random.randint(0, 10**12)}@test>"
    l = L(); l.append('"INBOX.Sent"', "(\\Seen)", imaplib.Time2Internaldate(time.time()),
                      f"From: {ACC}\r\nTo: x@y.com\r\nDate: {email.utils.formatdate()}\r\nSubject: sent\r\n"
                      f"Message-ID: {smid}\r\n\r\nhello\r\n".encode())
    lat = wait(lambda: find(H(), "INBOX.Sent", smid), 30)
    time.sleep(70)   # let pull's other-folder cycle see the new Hostinger Sent message
    n_h, n_l = len(find(H(), "INBOX.Sent", smid)), len(find(L(), "INBOX.Sent", smid))
    report("sent mail uploaded to Hostinger once, no local duplicate",
           lat is not None and n_h == 1 and n_l == 1, f"(uploaded after {lat:.1f}s; Hostinger {n_h}, local {n_l})"
           if lat is not None else "")

    # 7. DRAFTS -- simulate exactly what Roundcube / a phone does
    D, T, S = "INBOX.Drafts", "INBOX.Trash", "INBOX.Sent"

    def draft_raw(mid, text):
        return (f"From: {ACC}\r\nTo: client@ex.com\r\nSubject: offer\r\nDate: {email.utils.formatdate()}\r\n"
                f"Message-ID: {mid}\r\n\r\n{text}\r\n").encode()

    def save_draft(conn, mid, text):
        """Roundcube/phone 'Save draft': append new version, delete old one."""
        old = find(conn, D, mid)
        conn.append(f'"{D}"', "(\\Draft \\Seen)", None, draft_raw(mid, text))
        if old:
            conn.select(f'"{D}"'); conn.uid("STORE", ",".join(map(str, old)), "+FLAGS", "(\\Deleted)")
            conn.expunge()

    def bodies(conn, folder, mid):
        uids = find(conn, folder, mid)
        out = []
        for u in uids:
            t, d = conn.uid("FETCH", str(u), "(BODY.PEEK[TEXT])")
            out.append(d[0][1].decode().strip())
        return out

    mid = f"<draft{random.randint(0, 10**12)}@test>"
    save_draft(L(), mid, "version 1")
    lat = wait(lambda: bodies(H(), D, mid) == ["version 1"], 30)
    report("draft saved in Roundcube -> appears in Hostinger Drafts", lat is not None,
           f"({lat:.1f}s)" if lat is not None else f"(Hostinger: {bodies(H(), D, mid)})")

    save_draft(L(), mid, "version 2")
    lat = wait(lambda: bodies(H(), D, mid) == ["version 2"], 30)
    time.sleep(3)
    hd, ht = bodies(H(), D, mid), bodies(H(), T, mid)
    TRASH_MODE = ENV.get("DRAFT_OLD_VERSIONS", "trash") == "trash"
    report("draft edited -> Hostinger Drafts has ONLY the new version; old version "
           + ("kept in Trash" if TRASH_MODE else "removed"),
           lat is not None and hd == ["version 2"] and ht == (["version 1"] if TRASH_MODE else []),
           f"(Drafts {hd}, Trash {ht})")

    l = L()   # Roundcube "Send": save to Sent (same Message-ID), then delete the draft
    l.append(f'"{S}"', "(\\Seen)", None, draft_raw(mid, "version 2"))
    du = find(l, D, mid); l.select(f'"{D}"'); l.uid("STORE", ",".join(map(str, du)), "+FLAGS", "(\\Deleted)"); l.expunge()
    lat = wait(lambda: not find(H(), D, mid), 30)
    time.sleep(5)
    hs, ht = len(find(H(), S, mid)), len(find(H(), T, mid))
    report("draft sent -> gone from Hostinger Drafts, in Hostinger Sent once"
           + (", draft copies kept in Trash" if TRASH_MODE else ", not in Trash"),
           lat is not None and hs == 1 and (ht == 2 if TRASH_MODE else ht == 0), f"(Sent {hs}, Trash {ht})")

    mid2 = f"<draft{random.randint(0, 10**12)}@test>"
    save_draft(L(), mid2, "throw away")
    wait(lambda: find(H(), D, mid2), 30)
    l = L(); du = find(l, D, mid2)[0]; l.select(f'"{D}"'); l.uid("MOVE", str(du), T)   # Roundcube Delete
    lat = wait(lambda: not find(H(), D, mid2) and find(H(), T, mid2), 30)
    report("draft deleted in Roundcube -> moved to Hostinger Trash (recoverable)", lat is not None,
           f"(Drafts {len(find(H(), D, mid2))}, Trash {len(find(H(), T, mid2))})")

    mid3 = f"<phone{random.randint(0, 10**12)}@test>"
    save_draft(H(), mid3, "from phone v1")
    lat = wait(lambda: bodies(L(), D, mid3) == ["from phone v1"], 90)
    report("draft written on phone -> appears in Roundcube", lat is not None,
           f"({lat:.1f}s)" if lat is not None else f"(local {bodies(L(), D, mid3)})")

    save_draft(H(), mid3, "from phone v2")
    lat = wait(lambda: bodies(L(), D, mid3) == ["from phone v2"], 90)
    time.sleep(8)
    hd, ld, ht = bodies(H(), D, mid3), bodies(L(), D, mid3), bodies(H(), T, mid3)
    report("draft edited on phone -> Roundcube shows new version, no duplicates anywhere",
           lat is not None and hd == ["from phone v2"] and ld == ["from phone v2"] and ht == [],
           f"(local {ld}, Hostinger {hd}, Hostinger Trash {ht})")

    h = H(); du = find(h, D, mid3)[0]; h.select(f'"{D}"'); h.uid("STORE", str(du), "+FLAGS", "(\\Deleted)"); h.expunge()
    lat = wait(lambda: not find(L(), D, mid3), 90)
    report("draft sent/deleted on phone -> removed from Roundcube", lat is not None,
           f"({lat:.1f}s)" if lat is not None else "")

    # 8. folders: created both ways; local folder delete leaves Hostinger alone
    l = L(); l.create('"INBOX.Projects"'); h = H(); h.create('"INBOX.FromPhone"')
    subprocess.run(["pkill", "-USR1", "-f", "nonexistent"])  # no-op
    print("   (waiting for folder discovery cycle...)", flush=True)
    ok_l = wait(lambda: "INBOX.Projects" in str(H().list()[1]), 700, 5)
    ok_r = wait(lambda: "INBOX.FromPhone" in str(L().list()[1]), 60, 2)
    report("new folders created on the other side", ok_l is not None and ok_r is not None)
    L().delete('"INBOX.Projects"')
    time.sleep(15)
    report("folder deleted locally still exists on Hostinger", "INBOX.Projects" in str(H().list()[1]))

    # 9. Hostinger safety guard
    sys.path.insert(0, SRC)
    os.environ.update(ENV)
    import common
    acc = common.Account(ACC, "hpw", "lpw", "127.0.0.1", 10993)
    r = common.remote_connect(acc); r.select("INBOX")
    blocked = []
    for name, fn in [("EXPUNGE", lambda: r.expunge()), ("CLOSE", lambda: r.close()),
                     ("DELETE folder", lambda: r.delete('"INBOX.Junk"')),
                     ("RENAME folder", lambda: r.rename('"INBOX.Junk"', '"INBOX.X"')),
                     ("UID MOVE", lambda: r.uid("MOVE", "1", "INBOX.Trash")),
                     ("UID COPY", lambda: r.uid("COPY", "1", "INBOX.Trash")),
                     ("UID EXPUNGE", lambda: r.uid("EXPUNGE", "1")),
                     ("STORE \\Deleted", lambda: r.uid("STORE", "1", "+FLAGS", "(\\Deleted)")),
                     ("STORE (non-UID)", lambda: r.store("1", "+FLAGS", "(\\Seen)"))]:
        try:
            r.select("INBOX")
            fn(); blocked.append((name, False))
        except common.ForbiddenCommand:
            blocked.append((name, True))
    report("guard blocks every delete/move command to Hostinger", all(b for _n, b in blocked),
           ", ".join(n for n, b in blocked if not b))

    # the Drafts-only permit must not open anything else
    pm = f"<permit{random.randint(0, 10**12)}@test>"
    hh = H(); hh.append('"INBOX.Drafts"', "(\\Draft)", None, draft_raw(pm, "x")); duid = find(hh, D, pm)[0]
    inbox_uid = find(hh, "INBOX", mid_mv)[0]
    esc = []
    r.select('"INBOX"')
    with r.permit_draft_cleanup([inbox_uid]):
        for name, fn in [("\\Deleted on INBOX message", lambda: r.uid("STORE", str(inbox_uid), "+FLAGS", "(\\Deleted)")),
                         ("MOVE from INBOX", lambda: r.uid("MOVE", str(inbox_uid), "INBOX.Trash"))]:
            try:
                fn(); esc.append(name)
            except common.ForbiddenCommand:
                pass
    r.select('"INBOX.Drafts"')
    for name, fn in [("draft without permit", lambda: r.uid("STORE", str(duid), "+FLAGS", "(\\Deleted)")),
                     ("permit but other UID", lambda: _with(r, [duid], lambda: r.uid("EXPUNGE", str(duid + 1)))),
                     ("permit but MOVE to Junk", lambda: _with(r, [duid], lambda: r.uid("MOVE", str(duid), "INBOX.Junk"))),
                     ("permit but plain EXPUNGE", lambda: _with(r, [duid], lambda: r.expunge())),
                     ("permit but UID range *", lambda: _with(r, [duid], lambda: r.uid("EXPUNGE", "1:*")))]:
        try:
            fn(); esc.append(name)
        except common.ForbiddenCommand:
            pass
    report("Drafts permit can't touch anything else (other folders/UIDs/commands)", not esc, ", ".join(esc))
    r.logout()

    # 11. optional: deleted on Hostinger -> local copy (only if switched on)
    if ENV.get("DELETE_LOCAL_ON_REMOTE_DELETE") == "1":
        mid = new_mail(subject="deleted on phone"); wait(lambda: find(L(), "INBOX", mid), 60)
        h = H(); ru = find(h, "INBOX", mid)[0]; h.select("INBOX")
        h.uid("STORE", str(ru), "+FLAGS", "(\\Deleted)"); h.expunge(); h0 -= 0
        gone = wait(lambda: not find(L(), "INBOX", mid), 90, 2)
        report("deleted on Hostinger -> removed locally (switch on)", gone is not None,
               f"({gone:.0f}s)" if gone is not None else "")
        h0 -= 1

    # 10. counts: Hostinger lost nothing
    h1 = count(H(), "INBOX")
    report("Hostinger INBOX never shrank", h1 >= h0, f"({h0} -> {h1})")
finally:
    for p in procs:
        p.terminate()
    for f in logs.values():
        f.close()

print(f"\n{sum(ok for _n, ok in results)}/{len(results)} passed")
