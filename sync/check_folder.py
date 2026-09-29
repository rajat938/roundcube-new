"""
Compare one folder on Hostinger with the local mirror and explain every
difference. Optionally download what is missing. Never changes Hostinger.

  docker exec mail-sync-pull python3 /app/check_folder.py EMAIL [FOLDER]
  docker exec mail-sync-pull python3 /app/check_folder.py EMAIL FOLDER --fetch-missing
  docker exec mail-sync-pull python3 /app/check_folder.py EMAIL FOLDER --fetch-missing --include-deleted

Every copy counts: if Hostinger has the same Message-ID twice, Roundcube
should have it twice too.

Categories:
  extra copies missing    Hostinger has more copies of a mail (same
                          Message-ID) than Roundcube -- --fetch-missing
                          downloads the missing copies
  deleted in Roundcube    was downloaded, later deleted/moved locally
                          (kept out on purpose; --include-deleted restores)
  never downloaded        a real gap -- --fetch-missing downloads these
"""
import sys
from collections import defaultdict

from common import (
    Database, HEADER_ITEM, SyncError, appendable_flags, check, chunks, flags_to_db,
    local_connect, msg_key, q, remote_connect, safe_logout, uid_fetch, uidset,
)
import pymysql
from common import DB, Account


def load_account(email_addr):
    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT email, hostinger_password, local_password, imap_host, imap_port "
                    "FROM mirror_accounts WHERE email=%s", (email_addr,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        sys.exit(f"No account {email_addr} in mirror_accounts")
    return Account(*row)


def index(conn, folder, readonly=True):
    check(*conn.select(q(folder), readonly=readonly), f"SELECT {folder}")
    out = []  # (uid, key, header_bytes)
    for r in uid_fetch(conn, "1:*", f"UID {HEADER_ITEM}"):
        out.append((r.uid, msg_key(r.data), r.data or b""))
    return out


def describe(header):
    import email
    m = email.message_from_bytes(header)
    return f"{(m.get('Date') or '?')[:31]:31}  {(m.get('Subject') or '(no subject)')[:60]}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        sys.exit(__doc__)
    email_addr, folder = args[0], (args[1] if len(args) > 1 else "INBOX")
    fetch, include_deleted = "--fetch-missing" in flags, "--include-deleted" in flags

    acc = load_account(email_addr)
    db = Database()
    remote, local = remote_connect(acc), local_connect(acc)
    try:
        r_idx, l_idx = index(remote, folder), index(local, folder)
        local_count = defaultdict(int)
        for _u, k, _h in l_idx:
            if k:
                local_count[k] += 1
        mapped = {int(u) for (u,) in db.fetchall(
            "SELECT remote_uid FROM sync_msg_map WHERE account_email=%s AND folder=%s", (acc.email, folder))}

        by_key = defaultdict(list)
        for uid, key, head in r_idx:
            by_key[key].append((uid, head))

        extra, deleted, never, nokey = [], [], [], []
        for key, items in by_key.items():
            if key is None:
                nokey.extend(items)
                continue
            items.sort()
            have = local_count.get(key, 0)
            if have >= len(items):
                continue
            if have:
                extra.extend(items[have:])     # Roundcube has some copies, not all
            elif any(u in mapped for u, _h in items):
                deleted.extend(items)          # was downloaded, then removed locally
            else:
                never.extend(items)

        print(f"\n{acc.email}  {folder}")
        print(f"  on Hostinger              {len(r_idx):6,}")
        print(f"  in Roundcube (local)      {len(l_idx):6,}")
        print(f"  ------------------------------------")
        print(f"  extra copies missing      {len(extra):6,}   (same Message-ID, Hostinger has more copies)")
        print(f"  deleted in Roundcube      {len(deleted):6,}   (downloaded, then deleted/moved locally)")
        print(f"  never downloaded          {len(never):6,}   (real gap)")
        if nokey:
            print(f"  unidentifiable            {len(nokey):6,}   (no Message-ID/Date/From/Subject)")
        for title, items in (("never downloaded", never), ("extra copies missing", extra),
                             ("deleted in Roundcube", deleted)):
            if items:
                print(f"\n  first {min(10, len(items))} {title}:")
                for _u, head in items[:10]:
                    print("    " + describe(head))

        todo = list(never) + list(extra) + (list(deleted) if include_deleted else [])
        if not fetch:
            if never or extra or deleted:
                print("\n  To download 'never downloaded' + 'extra copies missing':  add --fetch-missing"
                      "\n  To also bring back the ones deleted in Roundcube:  add --include-deleted")
            return
        if not todo:
            print("\n  Nothing to download.")
            return
        check(*remote.select(q(folder), readonly=True), "EXAMINE")
        done = 0
        rows = []
        for part in chunks([u for u, _h in todo], 25):
            for r in uid_fetch(remote, uidset(part), "UID FLAGS INTERNALDATE BODY.PEEK[]"):
                if not isinstance(r.data, (bytes, bytearray)):
                    continue
                date = f'"{r.internaldate}"' if r.internaldate else None
                check(*local.append(q(folder), appendable_flags(r.flags), date, bytes(r.data)), "APPEND")
                key = msg_key(r.data)
                if key:
                    rows.append((acc.email, folder, r.uid, key, flags_to_db(r.flags)))
                done += 1
            print(f"  downloaded {done:,} / {len(todo):,}", flush=True)
        db.upsert_map(rows)
        print(f"\n  Done: {done:,} message(s) copied into Roundcube. Hostinger was not changed.")
    finally:
        safe_logout(remote)
        safe_logout(local)
        db.close()


if __name__ == "__main__":
    main()
