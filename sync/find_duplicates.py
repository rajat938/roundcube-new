"""
Explain the duplicates in one Hostinger folder -- READ-ONLY (EXAMINE), changes nothing.

  docker exec mail-sync-pull python3 /app/find_duplicates.py EMAIL [FOLDER] [--details]

For every Message-ID stored more than once it shows each copy's UID, the
date the SERVER stored it (INTERNALDATE), its size, and which headers differ.

How to read it:
  * UIDs only ever go up, in the order copies were ADDED to the folder.
    Copies with neighbouring UIDs were saved at the same moment (e.g. the
    sending program saved a copy twice). A copy whose UID is far higher was
    added much later (a sync tool, a migration/import, a re-upload).
  * "added around" = the date of the mail stored just before that copy --
    roughly WHEN the copy was put into the folder.
  * X-Mailer / User-Agent / X-* headers name the program that wrote it.
"""
import email
import re
import sys
from collections import defaultdict

import pymysql

from common import DB, Account, check, chunks, q, remote_connect, safe_logout

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    sys.exit(__doc__)
EMAIL, FOLDER = args[0], (args[1] if len(args) > 1 else "INBOX.Sent")
DETAILS = "--details" in sys.argv

conn = pymysql.connect(**DB)
cur = conn.cursor()
cur.execute("SELECT email, hostinger_password, local_password, imap_host, imap_port "
            "FROM mirror_accounts WHERE email=%s", (EMAIL,))
row = cur.fetchone()
conn.close()
if not row:
    sys.exit(f"No account {EMAIL}")

r = remote_connect(Account(*row))
try:
    check(*r.select(q(FOLDER), readonly=True), f"EXAMINE {FOLDER}")
    typ, data = r.uid("SEARCH", "ALL")
    uids = [int(x) for x in data[0].split()] if data and data[0] else []
    msgs = {}
    for part in chunks(uids, 300):
        typ, data = r.uid("FETCH", ",".join(map(str, part)),
                          "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER])")
        for it in data or []:
            if not isinstance(it, tuple):
                continue
            meta = it[0]
            m_uid = re.search(rb"UID (\d+)", meta)
            if not m_uid:
                continue
            m_date = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            m_size = re.search(rb"RFC822\.SIZE (\d+)", meta)
            hdr = email.message_from_bytes(it[1] or b"")
            msgs[int(m_uid.group(1))] = dict(
                date=m_date.group(1).decode() if m_date else "?",
                size=int(m_size.group(1)) if m_size else 0,
                mid=(hdr.get("Message-ID") or "").strip(),
                subject=str(hdr.get("Subject") or "")[:60],
                headers=[f"{k}: {str(v).strip()[:110]}" for k, v in hdr.items()])
finally:
    safe_logout(r)

order = sorted(msgs)
prev_date = {u: (msgs[order[i - 1]]["date"] if i else "-") for i, u in enumerate(order)}
groups = defaultdict(list)
for u in order:
    if msgs[u]["mid"]:
        groups[msgs[u]["mid"]].append(u)
dups = {mid: us for mid, us in groups.items() if len(us) > 1}

print(f"\n{EMAIL}  {FOLDER}: {len(msgs):,} mails, {len(dups)} Message-ID(s) stored more than once "
      f"({sum(len(v) - 1 for v in dups.values())} extra copies)\n")
together = later = 0
for n, (mid, us) in enumerate(sorted(dups.items(), key=lambda kv: kv[1][0])):
    positions = [order.index(u) for u in us]
    gap = max(positions) - min(positions)
    kind = "saved together" if gap <= 2 else "added LATER"
    together += gap <= 2
    later += gap > 2
    if n < (len(dups) if DETAILS else 10):
        print(f"{msgs[us[0]]['subject']}   [{kind}; {gap - 1 if gap else 0} other mails between the copies]")
        for u in us:
            m = msgs[u]
            print(f"   UID {u:>7}  stored {m['date']}  size {m['size']:>8,}  added around: {prev_date[u]}")
        sets = [set(msgs[u]["headers"]) for u in us]
        common_h = set.intersection(*sets)
        for u, s in zip(us, sets):
            extra = sorted(s - common_h)
            if extra:
                print(f"   only in UID {u}:")
                for h in extra[:8]:
                    print(f"      {h}")
        print()
if len(dups) > 10 and not DETAILS:
    print(f"... {len(dups) - 10} more (add --details to see all)\n")
print(f"SUMMARY: {together} saved together (same moment -> the sending program/server saved twice), "
      f"{later} added later (-> a sync tool, import or re-upload added a second copy)")
