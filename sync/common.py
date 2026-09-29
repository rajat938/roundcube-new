"""
Shared code for the two mail-sync services:

    pull.py  -- Hostinger -> local Dovecot   (new mail, flag changes, folders)
    push.py  -- local Dovecot -> Hostinger   (read/unread/flags, sent mail)

Works like a desktop mail client (Thunderbird / classic Outlook), not like
a mailbox diff:
  * new mail  = "give me UIDs above the last one I saw"      (one command)
  * changes   = "what changed since change-counter N?"       (CONDSTORE)
  * a few long-lived logins per account, not new logins every minute

HOSTINGER SAFETY RULE
---------------------
Every connection to Hostinger is a SafeRemoteIMAP. It refuses, before
anything is sent on the wire, every IMAP command that can remove or move
mail or folders: EXPUNGE, CLOSE, DELETE, RENAME, MOVE, COPY, plain STORE,
and any UID STORE touching a flag other than \\Seen \\Answered \\Flagged.
What can change on Hostinger: a message ADDED (sent mail, drafts), a
folder ADDED, read/answered/flagged state -- and, in the Drafts folder
ONLY, the old copy of a draft being replaced (see permit_draft_cleanup).
"""
import email
import email.parser
import hashlib
import imaplib
import logging
import os
import re
import ssl
import threading
import time
from contextlib import contextmanager

import pymysql

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mailsync")


# ----------------------------------------------------------------------
#  Settings (all from environment / .env)
# ----------------------------------------------------------------------
def _env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"{name} is not set -- put it in the .env file "
                           f"and pass it to this container.")
    return value


def _env_bool(name, default):
    return str(_env(name, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


DB = dict(host=_env("DB_HOST", required=True),
          user=_env("DB_USER", required=True),
          password=_env("DB_PASSWORD", required=True),
          database=_env("DB_NAME", required=True),
          charset="utf8mb4", autocommit=True)

LOCAL_IMAP_HOST = _env("LOCAL_IMAP_HOST", "dovecot")
LOCAL_IMAP_PORT = int(_env("LOCAL_IMAP_PORT", "143"))
# Hostinger has a real certificate -- keep verification ON in production.
REMOTE_TLS_VERIFY = _env_bool("REMOTE_TLS_VERIFY", True)
# The ONLY servers a Hostinger password may ever be sent to. Even if
# someone edits mirror_accounts.imap_host in the database, the sync
# refuses to log in anywhere else (stops password theft via the DB).
REMOTE_ALLOWED_HOSTS = {h.strip().lower() for h in
                        _env("REMOTE_ALLOWED_HOSTS", "imap.hostinger.com").split(",") if h.strip()}

SENT_FOLDER = _env("SENT_FOLDER", "INBOX.Sent")
DRAFTS_FOLDER = _env("DRAFTS_FOLDER", "INBOX.Drafts")
TRASH_FOLDER = _env("TRASH_FOLDER", "INBOX.Trash")

# how quickly Start/Stop from the admin page takes effect
ACCOUNT_RELOAD_INTERVAL = int(_env("ACCOUNT_RELOAD_INTERVAL", "10"))

TRACKED_FLAGS = ("\\Seen", "\\Answered", "\\Flagged")
_TRACKED_LOWER = {f.lower(): f for f in TRACKED_FLAGS}

HEADER_ITEM = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)]"


class SyncError(Exception):
    """A server said NO/BAD or returned something we can't trust."""


# ----------------------------------------------------------------------
#  Hostinger connection that physically cannot delete or move anything
# ----------------------------------------------------------------------
class ForbiddenCommand(Exception):
    pass


_REMOTE_ALLOWED = {
    "CAPABILITY", "LOGIN", "AUTHENTICATE", "LOGOUT", "NOOP", "ID", "NAMESPACE",
    "ENABLE", "SELECT", "EXAMINE", "UNSELECT", "STATUS", "LIST", "LSUB",
    "SEARCH", "FETCH", "APPEND", "CREATE", "SUBSCRIBE", "UID",
}
_REMOTE_UID_ALLOWED = {"SEARCH", "FETCH", "STORE"}
_STORE_OPS = {"+FLAGS", "-FLAGS", "+FLAGS.SILENT", "-FLAGS.SILENT"}


def _check_remote_store(args):
    if len(args) != 3:
        raise ForbiddenCommand(f"UID STORE with unexpected arguments: {args!r}")
    op, flags = str(args[1]).upper(), str(args[2])
    if op not in _STORE_OPS:
        raise ForbiddenCommand(f"UID STORE {op} is not allowed on Hostinger")
    tokens = flags.strip("()").split()
    if not tokens or any(t.lower() not in _TRACKED_LOWER for t in tokens):
        raise ForbiddenCommand(f"UID STORE of {flags} is not allowed on Hostinger "
                               f"(only {' '.join(TRACKED_FLAGS)})")


def _expand_uidset(value):
    """'3,7:9' -> {3,7,8,9}; None if it contains '*' or anything odd."""
    out = set()
    for part in str(value).split(","):
        if not re.fullmatch(r"\d+(:\d+)?", part):
            return None
        a, _, b = part.partition(":")
        lo, hi = int(a), int(b or a)
        if lo > hi:
            lo, hi = hi, lo
        if hi - lo > 10000:
            return None
        out.update(range(lo, hi + 1))
    return out


class SafeRemoteIMAP(imaplib.IMAP4_SSL):
    """imaplib connection to Hostinger with a hard allow-list. Every
    command imaplib sends goes through _command(), so this is the single
    choke point -- a bug elsewhere in this code base still cannot remove
    or move mail on Hostinger.

    ONE narrow exception, for draft sync only: inside
    `with conn.permit_draft_cleanup(uids):` the drafts code may, in the
    Drafts folder only, for exactly those UIDs only:
      * UID MOVE / UID COPY them to the Trash folder, or
      * UID STORE +FLAGS (\\Deleted) and UID EXPUNGE them (only used for
        an out-of-date copy of a draft that this sync uploaded itself).
    Plain EXPUNGE/CLOSE stay blocked, so no other message can ever be
    expunged, and nothing outside Drafts can be touched."""

    _permit = frozenset()
    _selected = None

    def select(self, mailbox="INBOX", readonly=False):
        self._selected = None
        typ, data = super().select(mailbox, readonly)
        if typ == "OK":
            self._selected = (mailbox, bool(readonly))
        return typ, data

    @contextmanager
    def permit_draft_cleanup(self, uids):
        self._permit = frozenset(int(u) for u in uids)
        try:
            yield
        finally:
            self._permit = frozenset()

    def _check_draft_cleanup(self, sub, args):
        if not self._permit:
            raise ForbiddenCommand(f"UID {sub} is not allowed on Hostinger")
        if self._selected != (q(DRAFTS_FOLDER), False):
            raise ForbiddenCommand(f"UID {sub} is only allowed in {DRAFTS_FOLDER} (read-write)")
        uids = _expand_uidset(args[0]) if args else None
        if not uids or not uids <= self._permit:
            raise ForbiddenCommand(f"UID {sub} on UIDs not registered for draft cleanup: {args[:1]}")
        if sub in ("MOVE", "COPY"):
            dest = str(args[1]) if len(args) > 1 else ""
            if dest not in (TRASH_FOLDER, q(TRASH_FOLDER)):
                raise ForbiddenCommand(f"UID {sub} is only allowed into {TRASH_FOLDER}")
        elif sub == "STORE":
            if (len(args) != 3 or str(args[1]).upper() not in ("+FLAGS", "+FLAGS.SILENT")
                    or str(args[2]).strip("()").strip().lower() != "\\deleted"):
                raise ForbiddenCommand(f"UID STORE {args[1:]} is not allowed for draft cleanup")

    def _command(self, name, *args):
        cmd = str(name).upper()
        if cmd not in _REMOTE_ALLOWED:
            raise ForbiddenCommand(f"{cmd} is not allowed on Hostinger")
        if cmd == "UID":
            sub = str(args[0]).upper() if args else ""
            rest = args[1:]
            if sub in ("MOVE", "COPY", "EXPUNGE"):
                self._check_draft_cleanup(sub, rest)
            elif sub == "STORE" and len(rest) == 3 and "\\deleted" in str(rest[2]).lower():
                self._check_draft_cleanup(sub, rest)
            elif sub not in _REMOTE_UID_ALLOWED:
                raise ForbiddenCommand(f"UID {sub} is not allowed on Hostinger")
            elif sub == "STORE":
                _check_remote_store(rest)
        return super()._command(name, *args)


_IDLE_ALLOWED = {"CAPABILITY", "LOGIN", "AUTHENTICATE", "EXAMINE", "IDLE", "NOOP", "LOGOUT", "ID",
                 "NAMESPACE", "ENABLE"}


def lock_down_idle_client(client):
    """The IDLE watcher uses IMAPClient (not SafeRemoteIMAP). Put the same
    kind of hard allow-list on it: login, read-only EXAMINE, IDLE, NOOP,
    LOGOUT. (IDLE only waits for server notifications; DONE ends it.)"""
    imap = client._imap
    original = imap._command

    def guarded(name, *args):
        if str(name).upper() not in _IDLE_ALLOWED:
            raise ForbiddenCommand(f"{name} is not allowed on the IDLE connection")
        return original(name, *args)

    imap._command = guarded
    return client


def remote_ssl_context():
    """TLS 1.2+, certificate AND hostname verified (Python defaults,
    made explicit so nobody weakens them by accident)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not REMOTE_TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


if not REMOTE_TLS_VERIFY:
    log.warning("!!! REMOTE_TLS_VERIFY=0: Hostinger's certificate is NOT checked -- anyone on the "
                "network path could impersonate Hostinger and capture passwords. Test setups only !!!")


def check_remote_host(acc):
    host = (acc.imap_host or "").strip().lower()
    if host not in REMOTE_ALLOWED_HOSTS:
        raise ForbiddenCommand(f"refusing to send {acc.email}'s password to '{acc.imap_host}' -- "
                               f"not in REMOTE_ALLOWED_HOSTS ({', '.join(sorted(REMOTE_ALLOWED_HOSTS))})")


def remote_connect(acc):
    check_remote_host(acc)
    conn = SafeRemoteIMAP(acc.imap_host, int(acc.imap_port),
                          ssl_context=remote_ssl_context(), timeout=90)
    conn.login(acc.email, acc.hostinger_password)
    return conn


def local_connect(acc):
    conn = imaplib.IMAP4(LOCAL_IMAP_HOST, LOCAL_IMAP_PORT, timeout=90)
    conn.login(acc.email, acc.local_password)
    return conn


def safe_logout(conn):
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        pass


def capabilities(conn):
    typ, data = conn.capability()
    if typ != "OK" or not data or not data[0]:
        return set()
    raw = data[0].decode() if isinstance(data[0], bytes) else data[0]
    return set(raw.upper().split())


def enable_condstore(conn):
    """True if the server supports CONDSTORE (per-folder change counter)."""
    if "CONDSTORE" not in capabilities(conn):
        return False
    try:
        conn.enable("CONDSTORE")
    except Exception:
        pass  # STATUS HIGHESTMODSEQ / CHANGEDSINCE enable it implicitly anyway
    return True


# ----------------------------------------------------------------------
#  IMAP helpers
# ----------------------------------------------------------------------
def q(mailbox):
    """Quote a mailbox name / search string for imaplib (it does not quote
    by itself). CR, LF and NUL are refused: they could end the IMAP
    command early and smuggle a second command to the server."""
    if any(ch in mailbox for ch in "\r\n\x00"):
        raise SyncError(f"refusing unsafe name {mailbox!r}")
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check(typ, data, what):
    if typ != "OK":
        detail = data[0] if data else b""
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise SyncError(f"{what} failed: {typ} {detail}")
    return data


def uidset(uids):
    """[1,2,3,7,9,10] -> '1:3,7,9:10'"""
    uids = sorted(set(int(u) for u in uids))
    parts, start, prev = [], None, None
    for u in uids:
        if start is None:
            start = prev = u
        elif u == prev + 1:
            prev = u
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = u
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


def chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def uid_search(conn, *criteria):
    typ, data = conn.uid("SEARCH", *criteria)
    check(typ, data, "UID SEARCH")
    if not data or not data[0]:
        return []
    return [int(x) for x in data[0].split()]


_STATUS_BODY_RE = re.compile(rb"\(([^()]*)\)\s*$")
_STATUS_PAIR_RE = re.compile(rb"([A-Z]+) (\d+)")


def status(conn, folder, items):
    """{'UIDNEXT': 5, ...} or None if the folder can't be STATUSed."""
    try:
        typ, data = conn.status(q(folder), f"({items})")
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data:
        return None
    line = data[-1]
    if isinstance(line, tuple):
        line = b" ".join(p for p in line if isinstance(p, bytes))
    m = _STATUS_BODY_RE.search(line or b"")
    if not m:
        return None
    return {k.decode(): int(v) for k, v in _STATUS_PAIR_RE.findall(m.group(1).upper())}


_FETCH_START_RE = re.compile(rb"^\d+ \(")
_UID_RE = re.compile(rb"\bUID (\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS \(([^)]*)\)")
_MODSEQ_RE = re.compile(rb"\bMODSEQ \((\d+)\)")
_IDATE_RE = re.compile(rb'\bINTERNALDATE "([^"]+)"')


class FetchRecord:
    __slots__ = ("uid", "flags", "modseq", "internaldate", "data", "key")

    def __init__(self, uid, flags, modseq, internaldate, data):
        self.uid, self.flags, self.modseq = uid, flags, modseq
        self.internaldate, self.data, self.key = internaldate, data, None


def parse_fetch(items):
    """Turn imaplib's flat FETCH response list into FetchRecords.

    Message with a literal: a (prefix, literal) tuple, maybe followed by a
    bytes item with the rest of that message's attributes (servers may
    send FLAGS after the literal). Message without a literal: one bytes
    item "N (UID .. FLAGS (..))". Responses without UID (unsolicited
    flag updates for other messages) are skipped."""
    raw = []
    for it in items or []:
        if isinstance(it, tuple) and len(it) >= 2:
            raw.append([bytes(it[0] or b""), it[1]])
        elif isinstance(it, (bytes, bytearray)):
            it = bytes(it)
            if _FETCH_START_RE.match(it):
                raw.append([it, None])
            elif raw:
                raw[-1][0] += b" " + it
    out = []
    for meta, literal in raw:
        m_uid = _UID_RE.search(meta)
        if not m_uid:
            continue
        m_flags = _FLAGS_RE.search(meta)
        flags = frozenset(m_flags.group(1).decode(errors="ignore").split()) if m_flags else None
        m_mod = _MODSEQ_RE.search(meta)
        m_date = _IDATE_RE.search(meta)
        out.append(FetchRecord(int(m_uid.group(1)), flags,
                               int(m_mod.group(1)) if m_mod else None,
                               m_date.group(1).decode() if m_date else None,
                               literal))
    return out


def uid_fetch(conn, uids_or_range, items, changedsince=None):
    args = [uids_or_range, f"({items})"]
    if changedsince is not None:
        args.append(f"(CHANGEDSINCE {int(changedsince)})")
    typ, data = conn.uid("FETCH", *args)
    check(typ, data, "UID FETCH")
    return parse_fetch(data)


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$')


def list_folders(conn):
    """Set of selectable folder names, or None if LIST failed (never
    confuse "couldn't list" with "no folders")."""
    try:
        typ, data = conn.list()
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data:
        return None
    names = set()
    for entry in data:
        if entry is None:
            continue
        literal_name = None
        if isinstance(entry, tuple):  # name sent as a literal
            entry, literal_name = entry[0], entry[1]
        m = _LIST_RE.match(entry)
        if not m:
            continue
        flags = m.group("flags").lower()
        if b"\\noselect" in flags or b"\\nonexistent" in flags:
            continue
        if literal_name is not None:
            name = literal_name
        else:
            name = m.group("name").strip()
            if name.startswith(b'"') and name.endswith(b'"'):
                name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        names.add(name.decode("utf-8", errors="replace"))
    return names


# ----------------------------------------------------------------------
#  Message identity + flags
# ----------------------------------------------------------------------
_MESSAGE_ID_RE = re.compile(r'^<[^\s"\\<>\x00-\x1f\x7f]+>$')
_header_parser = email.parser.BytesHeaderParser()


def msg_key(raw):
    """Stable identity of a message across the two servers: its
    Message-ID, or (if it has none) a hash of Date/From/Subject.
    `raw` may be just the header block or the whole message."""
    if not raw:
        return None
    msg = _header_parser.parsebytes(bytes(raw))
    mid = (msg.get("Message-ID") or "").strip()
    if mid and _MESSAGE_ID_RE.match(mid):
        return mid if len(mid) <= 190 else "h-" + hashlib.sha1(mid.encode()).hexdigest()
    basis = "|".join(str(msg.get(h, "")).strip() for h in ("Date", "From", "Subject"))
    if not basis.strip("|"):
        return None
    return "x-" + hashlib.sha1(basis.encode("utf-8", errors="replace")).hexdigest()


def content_hash(raw):
    """Hash of a message's content, independent of CRLF/LF storage."""
    data = bytes(raw or b"").replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def is_message_id(key):
    return bool(key) and key.startswith("<")


def tracked(flags):
    out = set()
    for f in flags or ():
        canon = _TRACKED_LOWER.get(f.lower())
        if canon:
            out.add(canon)
    return frozenset(out)


def flags_to_db(flags):
    return " ".join(sorted(tracked(flags)))


def flags_from_db(value):
    return tracked((value or "").split())


# Flags that may travel to Hostinger with an uploaded message. Anything else
# -- above all \Deleted (Roundcube's "flag for deletion" mode) -- is dropped,
# so an upload can never arrive on Hostinger already marked for deletion.
_UPLOAD_FLAGS = {"\\seen": "\\Seen", "\\answered": "\\Answered", "\\flagged": "\\Flagged",
                 "\\draft": "\\Draft"}


def upload_flags(flags):
    keep = sorted({_UPLOAD_FLAGS[f.lower()] for f in (flags or ()) if f.lower() in _UPLOAD_FLAGS})
    return f"({' '.join(keep)})" if keep else None


def appendable_flags(flags):
    """Flags for a copy written into the LOCAL mirror: everything except
    \Recent (server-managed) and \Deleted (a pending delete must not be
    mirrored -- the mail would vanish locally on the next expunge)."""
    keep = [f for f in (flags or ()) if f.lower() not in ("\\recent", "\\deleted")]
    return f"({' '.join(keep)})" if keep else None


_APPENDUID_RE = re.compile(rb"APPENDUID (\d+) (\d+)")


def appenduid(data):
    for d in data or []:
        if isinstance(d, bytes):
            m = _APPENDUID_RE.search(d)
            if m:
                return int(m.group(2))
    return None


# ----------------------------------------------------------------------
#  Database
# ----------------------------------------------------------------------
SCHEMA = [
    # Existing table (managed by account-manager) -- created here only so
    # a fresh database works.
    """
    CREATE TABLE IF NOT EXISTS mirror_accounts (
        id INT AUTO_INCREMENT PRIMARY KEY,
        email VARCHAR(255) UNIQUE NOT NULL,
        hostinger_password VARCHAR(255) NOT NULL,
        local_password VARCHAR(255) NOT NULL,
        imap_host VARCHAR(255) DEFAULT 'imap.hostinger.com',
        imap_port INT DEFAULT 993,
        active TINYINT DEFAULT 1,
        last_synced_at DATETIME DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Per-folder bookmarks. pull.py owns the remote_* / status columns,
    # push.py owns the local_* columns, so they never overwrite each other.
    """
    CREATE TABLE IF NOT EXISTS sync_folder_state (
        account_email VARCHAR(255) NOT NULL,
        folder VARCHAR(255) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        remote_uidvalidity BIGINT UNSIGNED DEFAULT NULL,
        remote_last_uid BIGINT UNSIGNED NOT NULL DEFAULT 0,
        remote_modseq BIGINT UNSIGNED NOT NULL DEFAULT 0,
        local_uidvalidity BIGINT UNSIGNED DEFAULT NULL,
        local_uidnext BIGINT UNSIGNED DEFAULT NULL,
        local_modseq BIGINT UNSIGNED DEFAULT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (account_email, folder)
    )
    """,
    # Which Hostinger message (folder + UID) is which message, and the
    # last flags both sides agreed on -- the baseline that lets each side
    # push only ITS OWN changes (no more read/unread tug-of-war).
    """
    CREATE TABLE IF NOT EXISTS sync_msg_map (
        account_email VARCHAR(255) NOT NULL,
        folder VARCHAR(255) NOT NULL,
        remote_uid BIGINT UNSIGNED NOT NULL,
        msg_key VARCHAR(200) NOT NULL,
        flags VARCHAR(100) NOT NULL DEFAULT '',
        PRIMARY KEY (account_email, folder, remote_uid),
        KEY k_msg (account_email, folder, msg_key)
    )
    """,
    # One row per draft (by Message-ID): which Hostinger UID holds its
    # current version, a hash of that version, and who created that copy
    # ('sync' = uploaded by sync-push, 'hostinger' = came from Hostinger).
    """
    CREATE TABLE IF NOT EXISTS sync_draft_state (
        account_email VARCHAR(255) NOT NULL,
        msg_key VARCHAR(200) NOT NULL,
        remote_uid BIGINT UNSIGNED DEFAULT NULL,
        content_hash CHAR(64) NOT NULL,
        origin VARCHAR(10) NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (account_email, msg_key)
    )
    """,
    # Progress per folder, written by sync-pull, shown in the admin page.
    """
    CREATE TABLE IF NOT EXISTS sync_progress (
        account_email VARCHAR(255) NOT NULL,
        folder VARCHAR(255) NOT NULL,
        remote_messages INT DEFAULT NULL,
        local_messages INT DEFAULT NULL,
        pending INT NOT NULL DEFAULT 0,
        start_pending INT NOT NULL DEFAULT 0,
        started_at DATETIME DEFAULT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (account_email, folder)
    )
    """,
    # What each service is doing right now, per account (+ heartbeat).
    """
    CREATE TABLE IF NOT EXISTS sync_account_status (
        account_email VARCHAR(255) NOT NULL PRIMARY KEY,
        pull_state VARCHAR(20) DEFAULT NULL,
        pull_detail VARCHAR(255) DEFAULT NULL,
        pull_heartbeat DATETIME DEFAULT NULL,
        push_state VARCHAR(20) DEFAULT NULL,
        push_detail VARCHAR(255) DEFAULT NULL,
        push_heartbeat DATETIME DEFAULT NULL,
        last_new_mail_at DATETIME DEFAULT NULL
    )
    """,
    # Global switches set from the admin page (name='paused' -> '1').
    """
    CREATE TABLE IF NOT EXISTS sync_settings (
        name VARCHAR(64) NOT NULL PRIMARY KEY,
        value VARCHAR(255) NOT NULL
    )
    """,
    # Actions a safety guard is holding back (shown in the admin page with an
    # Approve button). count = how many items; first_seen restarts whenever
    # the count changes, so "stable for N minutes" means really stable.
    """
    CREATE TABLE IF NOT EXISTS sync_guard (
        account_email VARCHAR(255) NOT NULL,
        guard VARCHAR(64) NOT NULL,
        message VARCHAR(500) NOT NULL,
        item_count INT NOT NULL DEFAULT 0,
        auto_minutes INT DEFAULT NULL,
        first_seen DATETIME NOT NULL,
        approved TINYINT NOT NULL DEFAULT 0,
        PRIMARY KEY (account_email, guard)
    )
    """,
    # Existing table from the old daemon; reused so already-uploaded sent
    # mail is never uploaded twice after migrating.
    """
    CREATE TABLE IF NOT EXISTS sent_upload_state (
        id INT AUTO_INCREMENT PRIMARY KEY,
        account_email VARCHAR(255) NOT NULL,
        message_id VARCHAR(512) NOT NULL,
        remote_verified TINYINT DEFAULT 0,
        local_removed TINYINT DEFAULT 0,
        uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_sent (account_email, message_id(191))
    )
    """,
]


class Database:
    """One connection per thread, reconnecting automatically."""

    def __init__(self):
        self._conn = None

    def cursor(self):
        if self._conn is None:
            self._conn = pymysql.connect(**DB)
        else:
            self._conn.ping(reconnect=True)
        return self._conn.cursor()

    def execute(self, sql, args=None):
        cur = self.cursor()
        cur.execute(sql, args)
        return cur

    def executemany(self, sql, rows):
        rows = list(rows)
        if not rows:
            return
        cur = self.cursor()
        for part in chunks(rows, 500):
            cur.executemany(sql, part)

    def fetchall(self, sql, args=None):
        return self.execute(sql, args).fetchall()

    def fetchone(self, sql, args=None):
        return self.execute(sql, args).fetchone()

    def close(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    # --- folder state -------------------------------------------------
    def folder_state(self, account, folder):
        row = self.fetchone(
            "SELECT status, remote_uidvalidity, remote_last_uid, remote_modseq, "
            "local_uidvalidity, local_uidnext, local_modseq "
            "FROM sync_folder_state WHERE account_email=%s AND folder=%s",
            (account, folder))
        if row is None:
            return None
        keys = ("status", "remote_uidvalidity", "remote_last_uid", "remote_modseq",
                "local_uidvalidity", "local_uidnext", "local_modseq")
        return dict(zip(keys, row))

    def folder_states(self, account):
        rows = self.fetchall("SELECT folder, status FROM sync_folder_state WHERE account_email=%s",
                             (account,))
        return {f: s for f, s in rows}

    def set_folder(self, account, folder, **cols):
        names = list(cols)
        sql = (f"INSERT INTO sync_folder_state (account_email, folder{''.join(', ' + n for n in names)}) "
               f"VALUES (%s, %s{', %s' * len(names)}) "
               f"ON DUPLICATE KEY UPDATE " + (", ".join(f"{n}=VALUES({n})" for n in names)
                                              if names else "folder=folder"))
        self.execute(sql, (account, folder, *[cols[n] for n in names]))

    # --- message map --------------------------------------------------
    def map_rows_by_uid(self, account, folder, uids):
        out = {}
        for part in chunks(uids, 500):
            rows = self.fetchall(
                "SELECT remote_uid, msg_key, flags FROM sync_msg_map WHERE account_email=%s "
                f"AND folder=%s AND remote_uid IN ({','.join(['%s'] * len(part))})",
                (account, folder, *part))
            for uid, key, flags in rows:
                out[int(uid)] = (key, flags_from_db(flags))
        return out

    def map_rows_by_key(self, account, folder, keys):
        out = {}
        for part in chunks([k for k in set(keys) if k], 500):
            rows = self.fetchall(
                "SELECT remote_uid, msg_key, flags FROM sync_msg_map WHERE account_email=%s "
                f"AND folder=%s AND msg_key IN ({','.join(['%s'] * len(part))})",
                (account, folder, *part))
            for uid, key, flags in rows:
                out.setdefault(key, []).append((int(uid), flags_from_db(flags)))
        return out

    def upsert_map(self, rows):
        """rows: (account, folder, remote_uid, msg_key, flags_str)"""
        self.executemany(
            "INSERT INTO sync_msg_map (account_email, folder, remote_uid, msg_key, flags) "
            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE msg_key=VALUES(msg_key), "
            "flags=VALUES(flags)", rows)

    def set_baseline(self, account, folder, remote_uids, flags):
        for part in chunks(remote_uids, 500):
            self.execute(
                "UPDATE sync_msg_map SET flags=%s WHERE account_email=%s AND folder=%s "
                f"AND remote_uid IN ({','.join(['%s'] * len(part))})",
                (flags_to_db(flags), account, folder, *part))


    # --- progress / status (read by the admin page) ----------------------
    def set_progress(self, account, folder, **cols):
        if cols.pop("started_now", False):
            cols["started_at"] = None  # placeholder, replaced by NOW() below
            names = list(cols)
            vals = ["NOW()" if n == "started_at" else "%s" for n in names]
            args = [cols[n] for n in names if n != "started_at"]
        else:
            names = list(cols)
            vals = ["%s"] * len(names)
            args = [cols[n] for n in names]
        sql = (f"INSERT INTO sync_progress (account_email, folder, {', '.join(names)}) "
               f"VALUES (%s, %s, {', '.join(vals)}) ON DUPLICATE KEY UPDATE "
               + ", ".join(f"{n}=VALUES({n})" for n in names))
        self.execute(sql, (account, folder, *args))

    def set_status(self, account, service, state, detail=""):
        """service: 'pull' or 'push'. Also refreshes the heartbeat."""
        assert service in ("pull", "push")
        self.execute(
            f"INSERT INTO sync_account_status (account_email, {service}_state, {service}_detail, "
            f"{service}_heartbeat) VALUES (%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE "
            f"{service}_state=VALUES({service}_state), {service}_detail=VALUES({service}_detail), "
            f"{service}_heartbeat=NOW()", (account, state, (detail or "")[:255]))

    # --- safety guards -------------------------------------------------
    def guard_decide(self, account, guard, message, count, auto_minutes=None):
        """Called while a guard is tripped. Returns True when the held
        action may go ahead now: either the admin pressed Approve, or
        (auto_minutes set) the exact same situation has persisted that long
        -- a glitch doesn't last, a real bulk delete does."""
        row = self.fetchone(
            "SELECT item_count, approved, TIMESTAMPDIFF(SECOND, first_seen, NOW()) "
            "FROM sync_guard WHERE account_email=%s AND guard=%s", (account, guard))
        if row and row[1]:
            self.guard_clear(account, guard)
            return True
        if row and int(row[0]) == int(count):
            if auto_minutes is not None and row[2] is not None and row[2] >= auto_minutes * 60:
                self.guard_clear(account, guard)
                return True
            self.execute("UPDATE sync_guard SET message=%s WHERE account_email=%s AND guard=%s",
                         (message[:500], account, guard))
            return False
        # new, or the number changed -> (re)start the clock
        self.execute(
            "INSERT INTO sync_guard (account_email, guard, message, item_count, auto_minutes, first_seen, approved) "
            "VALUES (%s,%s,%s,%s,%s,NOW(),0) ON DUPLICATE KEY UPDATE message=VALUES(message), "
            "item_count=VALUES(item_count), auto_minutes=VALUES(auto_minutes), first_seen=NOW(), approved=0",
            (account, guard, message[:500], int(count), auto_minutes))
        return False

    def guard_clear(self, account, guard):
        self.execute("DELETE FROM sync_guard WHERE account_email=%s AND guard=%s", (account, guard))

    # --- drafts ---------------------------------------------------------
    def draft_states(self, account):
        rows = self.fetchall("SELECT msg_key, remote_uid, content_hash, origin FROM sync_draft_state "
                             "WHERE account_email=%s", (account,))
        return {k: (int(u) if u is not None else None, h, o) for k, u, h, o in rows}

    def set_draft_state(self, account, key, remote_uid, content_hash, origin):
        self.execute(
            "INSERT INTO sync_draft_state (account_email, msg_key, remote_uid, content_hash, origin) "
            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE remote_uid=VALUES(remote_uid), "
            "content_hash=VALUES(content_hash), origin=VALUES(origin)",
            (account, key, remote_uid, content_hash, origin))

    def delete_draft_state(self, account, key):
        self.execute("DELETE FROM sync_draft_state WHERE account_email=%s AND msg_key=%s", (account, key))

    def delete_map_uids(self, account, folder, uids):
        for part in chunks(uids, 500):
            self.execute(
                "DELETE FROM sync_msg_map WHERE account_email=%s AND folder=%s "
                f"AND remote_uid IN ({','.join(['%s'] * len(part))})", (account, folder, *part))


def ensure_schema():
    while True:
        try:
            conn = pymysql.connect(**DB)
            cur = conn.cursor()
            for stmt in SCHEMA:
                cur.execute(stmt)
            conn.close()
            log.info("[startup] database schema OK")
            return
        except Exception as e:
            log.warning("[startup] waiting for database (%s: %s)", type(e).__name__, e)
            time.sleep(5)


# ----------------------------------------------------------------------
#  Accounts + supervision
# ----------------------------------------------------------------------
class Account:
    __slots__ = ("email", "hostinger_password", "local_password", "imap_host", "imap_port")

    def __init__(self, email_addr, hpw, lpw, host, port):
        self.email, self.hostinger_password, self.local_password = email_addr, hpw, lpw
        self.imap_host, self.imap_port = host or "imap.hostinger.com", int(port or 993)

    def fingerprint(self):
        return hashlib.sha256("\0".join([self.email, self.hostinger_password, self.local_password,
                                         self.imap_host, str(self.imap_port)]).encode()).hexdigest()


def load_accounts():
    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT email, hostinger_password, local_password, imap_host, imap_port "
                    "FROM mirror_accounts WHERE active=1")
        return [Account(*row) for row in cur.fetchall()]
    finally:
        conn.close()


def is_auth_error(exc):
    text = str(exc).upper()
    return isinstance(exc, imaplib.IMAP4.error) and not isinstance(exc, imaplib.IMAP4.abort) and (
        "AUTHENTICATIONFAILED" in text or "AUTHENTICATE" in text or "LOGIN" in text
        or "INVALID CREDENTIALS" in text or "PASSWORD" in text)


class Backoff:
    """5s, 10s, 20s ... up to 15 min. Wrong password starts at 5 min so we
    never hammer Hostinger with failing logins (account lock-out risk)."""

    def __init__(self):
        self.delay = 0

    def reset(self):
        self.delay = 0

    def next(self, exc=None):
        if exc is not None and is_auth_error(exc):
            self.delay = max(self.delay * 2, 300)
        else:
            self.delay = min(max(self.delay * 2, 5), 900)
        self.delay = min(self.delay, 900)
        return self.delay


def describe(exc):
    text = str(exc).replace("\n", " ")
    return f"{type(exc).__name__}: {text[:200]}"


def global_paused():
    """True when 'Stop all sync' was pressed in the admin page."""
    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM sync_settings WHERE name='paused'")
        row = cur.fetchone()
        return bool(row and row[0] == "1")
    finally:
        conn.close()


def supervise(service_name, start_account, service):
    """Keep one worker per active account. Restarts a worker whose
    thread died, stops it when the account is stopped in the admin page
    (or 'Stop all sync' is on), and restarts it when the password/host
    changes. Changes take effect within ACCOUNT_RELOAD_INTERVAL seconds.
    A new worker for an account is only started once the previous one
    has fully finished, so two workers never run for the same account."""
    ensure_schema()
    running = {}   # email -> (fingerprint, stop_event, thread)
    stopping = {}  # email -> thread still winding down
    was_paused = None
    db = Database()
    log.info("[%s] started", service_name)
    while True:
        try:
            paused = global_paused()
            wanted = {} if paused else {a.email: a for a in load_accounts()}
        except Exception as e:
            log.warning("[%s] cannot load accounts: %s", service_name, describe(e))
            time.sleep(ACCOUNT_RELOAD_INTERVAL)
            continue
        if paused != was_paused:
            log.info("[%s] sync is %s", service_name, "STOPPED from the admin page" if paused else "running")
            was_paused = paused

        for addr, thread in list(stopping.items()):
            if not thread.is_alive():
                del stopping[addr]
                if addr not in wanted:
                    try:
                        db.set_status(addr, service, "stopped",
                                      "all sync stopped by admin" if paused else "stopped by admin")
                    except Exception:
                        pass

        for addr, (fp, stop, thread) in list(running.items()):
            acc = wanted.get(addr)
            if acc is None:
                log.info("[%s] %s stopped/removed -- stopping worker", service_name, addr)
            elif acc.fingerprint() != fp:
                log.info("[%s] %s settings changed -- restarting", service_name, addr)
                stop.reason = "restarting (account settings changed)"
            elif not thread.is_alive():
                log.warning("[%s] %s worker stopped unexpectedly -- restarting", service_name, addr)
                stop.reason = "restarting"
            else:
                continue
            if not hasattr(stop, "reason"):
                stop.reason = "all sync stopped by admin" if paused else "stopped by admin"
            stop.set()
            del running[addr]
            if thread.is_alive():
                stopping[addr] = thread

        for addr, acc in wanted.items():
            if addr in running or addr in stopping:
                continue
            stop = threading.Event()
            thread = threading.Thread(target=start_account, args=(acc, stop),
                                      name=f"{service_name}:{addr}", daemon=True)
            thread.start()
            running[addr] = (acc.fingerprint(), stop, thread)

        time.sleep(ACCOUNT_RELOAD_INTERVAL)
