"""
sync-pull: Hostinger -> local Dovecot.

Per account it keeps exactly two Hostinger logins open:
  1. IDLE watcher on INBOX -- Hostinger tells us the moment mail arrives.
  2. Worker -- does the actual pulling, only when something changed:
       * new mail:    UID SEARCH UID <last_seen+1>:*   (only new messages)
       * flag change: UID FETCH 1:* (FLAGS) (CHANGEDSINCE <counter>)
       * other folders: one cheap STATUS per folder per minute; a folder
         is only opened when its UIDNEXT / HIGHESTMODSEQ moved.

Because it only ever asks for UIDs ABOVE the last one it saw, a message
you delete or move in Roundcube is NOT pulled back in again (the old
imapsync setup re-copied anything "missing" locally).

The worker only ever EXAMINEs (read-only) Hostinger folders, and the
connection is a SafeRemoteIMAP, so this service cannot change or delete
anything on Hostinger except creating a folder you created locally.
"""
import threading
import time

from imapclient import IMAPClient

from common import (
    Backoff, Database, DRAFTS_FOLDER, TRASH_FOLDER, SyncError, TRACKED_FLAGS, HEADER_ITEM, _env, _env_bool,
    lock_down_idle_client,
    appendable_flags, check, chunks, content_hash, describe, enable_condstore, flags_to_db,
    check_remote_host, is_message_id, list_folders, local_connect, log, msg_key, q, remote_connect,
    remote_ssl_context, safe_logout, status, supervise, tracked, uid_fetch,
    uid_search, uidset,
)

INBOX_CHECK_SECONDS = int(_env("INBOX_CHECK_SECONDS", "30"))          # safety net besides IDLE
OTHER_FOLDERS_SECONDS = int(_env("OTHER_FOLDERS_SECONDS", "60"))      # STATUS check of other folders
FOLDER_DISCOVERY_SECONDS = int(_env("FOLDER_DISCOVERY_SECONDS", "600"))
FLAG_RESCAN_SECONDS = int(_env("FLAG_RESCAN_SECONDS", "300"))         # only if Hostinger lacks CONDSTORE
REMOTE_DELETE_CHECK_SECONDS = int(_env("REMOTE_DELETE_CHECK_SECONDS", "300"))
# Never act on more than this many Hostinger-side deletions in one pass
# (a flaky/empty server answer must not empty the local mirror).
REMOTE_DELETE_MAX_PER_PASS = int(_env("REMOTE_DELETE_MAX_PER_PASS", "20"))
# A bulk delete on Hostinger that is still true after this many minutes of
# re-checks is real (a glitch doesn't last) -> go ahead. Or press Approve.
GUARD_CONFIRM_MINUTES = int(_env("GUARD_CONFIRM_MINUTES", "15"))
# Drafts disappearing from Hostinger: up to this many at once is normal.
DRAFT_VANISH_LIMIT = int(_env("DRAFT_VANISH_LIMIT", "3"))
IDLE_CHECK_SECONDS = int(_env("IDLE_CHECK_SECONDS", "20"))
IDLE_REFRESH_SECONDS = int(_env("IDLE_REFRESH_SECONDS", str(25 * 60)))
FORCE_NO_CONDSTORE = _env_bool("FORCE_NO_CONDSTORE", False)
SYNC_DRAFTS = _env_bool("SYNC_DRAFTS", True)

# Hostinger deleted a message from INBOX -> also delete the local copy?
# Default OFF: nothing is deleted anywhere; the local copy just stays.
DELETE_LOCAL_ON_REMOTE_DELETE = _env_bool("DELETE_LOCAL_ON_REMOTE_DELETE", False)

HEADER_BATCH = 500   # messages per header FETCH
BODY_BATCH = 25      # messages per full-body FETCH (keeps memory bounded)
INDEX_THRESHOLD = 40  # more lookups than this -> index the whole local folder once


class Puller:
    def __init__(self, acc, stop):
        self.acc, self.stop = acc, stop
        self.tag = f"[pull {acc.email}]"
        self.db = Database()
        self.remote = self.local = None
        self.remote_selected = self.local_selected = None
        self.condstore = False
        self.inbox_event = threading.Event()
        self._status = (None, None)
        self._status_at = 0
        self.problems = {}   # guard name -> message shown on the admin page
        self._progress_at = 0
        self._folder_progress_at = {}
        self._remote_total = None

    # ------------------------------------------------------------------
    def run(self):
        idle = threading.Thread(target=self.idle_watch, name=f"idle:{self.acc.email}", daemon=True)
        idle.start()
        backoff = Backoff()
        while not self.stop.is_set():
            try:
                self.status("connecting", "logging in to Hostinger")
                self.connect()
                backoff.reset()
                self.status("syncing", "checking all folders")
                self.discover_folders()
                self.sync_all_folders(first_pass=True)
                self.loop()
            except Exception as e:
                delay = backoff.next(e)
                log.warning("%s %s -- retrying in %ss", self.tag, describe(e), delay)
                self.status("error", f"{describe(e)} -- retrying in {delay}s")
                self.disconnect()
                self.stop.wait(delay)
        self.disconnect()
        idle.join(IDLE_CHECK_SECONDS + 15)  # never overlap with a restarted worker
        self.status("stopped", getattr(self.stop, "reason", "stopped"), force=True)
        self.db.close()
        log.info("%s stopped", self.tag)

    # ------------------------------------------------------------------
    #  Status / progress for the admin page (never allowed to break sync)
    # ------------------------------------------------------------------
    def status(self, state, detail="", force=False):
        now = time.time()
        if not force and (state, detail) == self._status and now - self._status_at < 30:
            return
        try:
            self.db.set_status(self.acc.email, "pull", state, detail)
            self._status, self._status_at = (state, detail), now
        except Exception as e:
            log.debug("%s status write failed: %s", self.tag, describe(e))

    def progress(self, folder, force=True, **cols):
        now = time.time()
        if not force and now - self._progress_at < 2:
            return
        try:
            self.db.set_progress(self.acc.email, folder, **cols)
            self._progress_at = now
        except Exception as e:
            log.debug("%s progress write failed: %s", self.tag, describe(e))

    def report_idle_status(self):
        if self.problems:
            self.status("error", " | ".join(self.problems.values()))
        else:
            self.status("up to date", "watching for new mail")

    def problem(self, name, message=None):
        """Set (message) or clear (None) a guard problem for the admin page."""
        before = dict(self.problems)
        if message and self.problems.get(name) != message:
            log.warning("%s %s", self.tag, message)
        if message:
            self.problems[name] = message
        else:
            self.problems.pop(name, None)
        if self.problems != before:
            self.report_idle_status()   # show / clear it on the admin page right away

    def local_count(self, folder):
        if self.local_selected == folder:
            self.local.noop()
        st = status(self.local, folder, "MESSAGES")
        return st.get("MESSAGES") if st else None

    def connect(self):
        self.disconnect()
        self.remote = remote_connect(self.acc)
        self.condstore = enable_condstore(self.remote) and not FORCE_NO_CONDSTORE
        self.local = local_connect(self.acc)
        enable_condstore(self.local)
        log.info("%s connected (Hostinger CONDSTORE: %s)", self.tag, "yes" if self.condstore else "no")

    def disconnect(self):
        safe_logout(self.remote)
        safe_logout(self.local)
        self.remote = self.local = None
        self.remote_selected = self.local_selected = None

    def loop(self):
        now = time.time()
        last_others = last_discovery = last_flag_rescan = last_delete_check = now
        while not self.stop.is_set():
            triggered = self.inbox_event.wait(INBOX_CHECK_SECONDS)
            if self.stop.is_set():
                return
            self.inbox_event.clear()
            self.sync_folder("INBOX", reason="new mail" if triggered else None)
            self.report_idle_status()

            now = time.time()
            if now - last_others >= OTHER_FOLDERS_SECONDS:
                self.sync_all_folders(skip_inbox=True)
                last_others = time.time()
                self.report_idle_status()
            if now - last_discovery >= FOLDER_DISCOVERY_SECONDS:
                self.discover_folders()
                last_discovery = time.time()
            if not self.condstore and now - last_flag_rescan >= FLAG_RESCAN_SECONDS:
                for folder in self.active_folders():
                    self.pull_flags(folder, since=None)
                last_flag_rescan = time.time()
            if now - last_delete_check >= REMOTE_DELETE_CHECK_SECONDS:
                self.check_remote_deletions("INBOX")
                last_delete_check = time.time()

    # ------------------------------------------------------------------
    #  IDLE watcher (login #1)
    # ------------------------------------------------------------------
    def idle_watch(self):
        backoff = Backoff()
        while not self.stop.is_set():
            try:
                check_remote_host(self.acc)
                with IMAPClient(self.acc.imap_host, port=self.acc.imap_port, ssl=True,
                                ssl_context=remote_ssl_context(), timeout=90) as client:
                    lock_down_idle_client(client)  # login / EXAMINE / NOOP / LOGOUT only
                    client.login(self.acc.email, self.acc.hostinger_password)
                    client.select_folder("INBOX", readonly=True)
                    backoff.reset()
                    self.inbox_event.set()  # catch anything that arrived while we were away
                    started = time.time()
                    while not self.stop.is_set() and time.time() - started < IDLE_REFRESH_SECONDS:
                        client.idle()
                        responses = client.idle_check(timeout=IDLE_CHECK_SECONDS)
                        _text, done = client.idle_done()
                        # EXISTS = new mail; FETCH = flags changed elsewhere
                        # (e.g. read on the phone); EXPUNGE = removed
                        if _has_changes(list(responses or []) + list(done or [])):
                            self.inbox_event.set()
            except Exception as e:
                delay = backoff.next(e)
                log.warning("%s IDLE watcher: %s -- reconnecting in %ss", self.tag, describe(e), delay)
                self.stop.wait(delay)

    # ------------------------------------------------------------------
    #  Folder selection helpers
    # ------------------------------------------------------------------
    def examine_remote(self, folder):
        if self.remote_selected != folder:
            self.remote_selected = None
            check(*self.remote.select(q(folder), readonly=True), f"EXAMINE {folder} on Hostinger")
            self.remote_selected = folder

    def select_local(self, folder):
        if self.local_selected == folder:
            # refresh the session's view so messages added by Roundcube or
            # sync-push since we opened the folder are visible to SEARCH
            # (otherwise the duplicate check could miss them)
            self.local.noop()
        else:
            self.local_selected = None
            check(*self.local.select(q(folder)), f"SELECT {folder} locally")
            self.local_selected = folder

    def active_folders(self):
        states = self.db.folder_states(self.acc.email)
        folders = [f for f, s in states.items() if s == "active"]
        return sorted(folders, key=lambda f: (f != "INBOX", f))

    # ------------------------------------------------------------------
    #  Per-folder sync
    # ------------------------------------------------------------------
    def sync_all_folders(self, first_pass=False, skip_inbox=False):
        for folder in self.active_folders():
            if self.stop.is_set():
                return
            if skip_inbox and folder == "INBOX":
                continue
            self.sync_folder(folder, reason="startup" if first_pass else None)

    def sync_folder(self, folder, reason=None):
        items = "MESSAGES UIDNEXT UIDVALIDITY" + (" HIGHESTMODSEQ" if self.condstore else "")
        if self.remote_selected == folder:
            # Dovecot answers STATUS for the folder this session has open
            # from the session's (stale) view until a NOOP refreshes it --
            # without this, new mail in INBOX was never noticed.
            self.remote.noop()
        st_remote = status(self.remote, folder, items)
        if st_remote is None:
            log.warning("%s cannot STATUS '%s' on Hostinger -- skipped this round", self.tag, folder)
            return
        state = self.db.folder_state(self.acc.email, folder) or {}
        uidvalidity = st_remote.get("UIDVALIDITY")
        last_uid = int(state.get("remote_last_uid") or 0)
        modseq = int(state.get("remote_modseq") or 0)

        if state.get("remote_uidvalidity") not in (None, uidvalidity):
            log.warning("%s '%s' UIDVALIDITY changed on Hostinger -- re-mapping folder "
                        "(existing local mail is matched, not duplicated)", self.tag, folder)
            self.db.execute("DELETE FROM sync_msg_map WHERE account_email=%s AND folder=%s",
                            (self.acc.email, folder))
            last_uid, modseq = 0, 0
        if state.get("remote_uidvalidity") != uidvalidity:
            self.db.set_folder(self.acc.email, folder, remote_uidvalidity=uidvalidity,
                               remote_last_uid=last_uid, remote_modseq=modseq)

        new_mail = st_remote.get("UIDNEXT", 0) - 1 > last_uid
        new_modseq = st_remote.get("HIGHESTMODSEQ")
        flag_changes = self.condstore and modseq and new_modseq and new_modseq > modseq

        is_drafts = SYNC_DRAFTS and folder == DRAFTS_FOLDER
        if is_drafts:
            # drafts removed on Hostinger (sent/deleted on the phone)
            self.check_draft_vanish()
        remote_total = st_remote.get("MESSAGES")
        if new_mail:
            started = time.time()
            self._remote_total = remote_total
            copied = self.pull_drafts(last_uid) if is_drafts else self.pull_new(folder, last_uid)
            if copied:
                log.info("%s %s: %d new message(s) copied in %.1fs%s", self.tag, folder, copied,
                         time.time() - started, f" ({reason})" if reason else "")
        if flag_changes:
            self.pull_flags(folder, since=modseq)
        if self.condstore and new_modseq and new_modseq != modseq:
            # value read BEFORE the work above -> anything changing meanwhile
            # is picked up next round
            self.db.set_folder(self.acc.email, folder, remote_modseq=new_modseq)
        if self.stop.is_set():
            return
        if new_mail or flag_changes or reason == "startup" or \
                time.time() - self._folder_progress_at.get(folder, 0) > 300:
            self.progress(folder, remote_messages=remote_total, local_messages=self.local_count(folder),
                          pending=0)
            self._folder_progress_at[folder] = time.time()

    def pull_new(self, folder, last_uid):
        """Copy Hostinger messages with UID > last_uid that aren't already
        in the local folder. Returns how many were copied."""
        self.examine_remote(folder)
        uids = [u for u in uid_search(self.remote, "UID", f"{last_uid + 1}:*") if u > last_uid]
        if not uids:
            return 0
        total = len(uids)
        big = total > 100
        self.progress(folder, remote_messages=self._remote_total, pending=total, start_pending=total,
                      started_now=True)
        if big:
            self.status("syncing", f"{folder}: downloading {total:,} message(s)", force=True)
        copied = processed = 0
        for part in chunks(uids, HEADER_BATCH):
            if self.stop.is_set():
                return copied  # bookmark = last finished batch; resumes there
            # UIDs sync-push created itself (sent mail it uploaded) are
            # already mapped -- never copy those back down
            known = self.db.map_rows_by_uid(self.acc.email, folder, part)
            recs = [r for r in uid_fetch(self.remote, uidset(part), f"UID FLAGS {HEADER_ITEM}")
                    if r.uid not in known]
            for r in recs:
                r.key = msg_key(r.data)
            # Match by COUNT, never collapse duplicates: if Hostinger has the
            # same Message-ID twice and Roundcube has it once, the second
            # copy is downloaded. A local copy counts as "already here" only
            # while it isn't already linked to another Hostinger copy.
            keys = [r.key for r in recs]
            copies = self.local_copies(folder, keys)
            taken = self.mapped_counts(folder, keys)

            map_rows, to_copy = [], []
            for r in recs:
                local = copies.get(r.key, []) if r.key else []
                n_taken = taken.get(r.key, 0)
                if r.key and n_taken < len(local):
                    luid, lflags = local[n_taken]
                    taken[r.key] = n_taken + 1
                    if r.flags is not None and tracked(lflags) != tracked(r.flags):
                        self.set_local_flags(folder, luid, lflags, r.flags)
                    map_rows.append((self.acc.email, folder, r.uid, r.key, flags_to_db(r.flags)))
                else:
                    to_copy.append(r)

            keys = {r.uid: r.key for r in to_copy}
            left_in_part = len(to_copy)
            for sub in chunks(to_copy, BODY_BATCH):
                if self.stop.is_set():
                    self.db.upsert_map(map_rows)
                    return copied
                bodies = uid_fetch(self.remote, uidset([r.uid for r in sub]),
                                   "UID FLAGS INTERNALDATE BODY.PEEK[]")
                for b in bodies:
                    if not isinstance(b.data, (bytes, bytearray)):
                        continue
                    date = f'"{b.internaldate}"' if b.internaldate else None
                    check(*self.local.append(q(folder), appendable_flags(b.flags), date, bytes(b.data)),
                          f"APPEND to local {folder}")
                    copied += 1
                    if keys.get(b.uid):
                        map_rows.append((self.acc.email, folder, b.uid, keys[b.uid], flags_to_db(b.flags)))
                left_in_part -= len(sub)
                remaining = total - processed - len(part) + left_in_part
                if time.time() - self._progress_at >= 2:
                    self.progress(folder, pending=remaining, local_messages=self.local_count(folder))
                if big:
                    self.status("syncing", f"{folder}: {remaining:,} of {total:,} left")

            self.db.upsert_map(map_rows)
            # bookmark after every batch -> a crash mid-backfill resumes here
            self.db.set_folder(self.acc.email, folder, remote_last_uid=max(part))
            processed += len(part)
            if time.time() - self._progress_at >= 2:
                self.progress(folder, pending=total - processed, local_messages=self.local_count(folder))
        return copied

    def pull_flags(self, folder, since):
        """Apply read/answered/flagged changes made on Hostinger (phone,
        other clients) to the local copy. since=None -> compare everything
        (only used when Hostinger has no CONDSTORE)."""
        self.examine_remote(folder)
        recs = uid_fetch(self.remote, "1:*", "UID FLAGS", changedsince=since)
        if not recs:
            return
        baseline = self.db.map_rows_by_uid(self.acc.email, folder, [r.uid for r in recs])
        changed = [(r, baseline[r.uid]) for r in recs
                   if r.flags is not None and r.uid in baseline and tracked(r.flags) != baseline[r.uid][1]]
        if not changed:
            return
        existing = self.local_lookup(folder, [key for _r, (key, _f) in changed])
        applied = 0
        for r, (key, _base) in changed:
            if key in existing:
                luid, lflags = existing[key]
                if tracked(lflags) != tracked(r.flags):
                    self.set_local_flags(folder, luid, lflags, r.flags)
                    applied += 1
            self.db.set_baseline(self.acc.email, folder, [r.uid], r.flags)
        if applied:
            log.info("%s %s: applied %d flag change(s) from Hostinger", self.tag, folder, applied)

    def check_remote_deletions(self, folder):
        """Notice messages that disappeared from Hostinger's folder. The
        local copy is only removed if DELETE_LOCAL_ON_REMOTE_DELETE=1."""
        state = self.db.folder_state(self.acc.email, folder) or {}
        last_uid = int(state.get("remote_last_uid") or 0)
        if not last_uid:
            return
        self.examine_remote(folder)
        present = set(uid_search(self.remote, "UID", f"1:{last_uid}"))
        mapped = self.db.fetchall(
            "SELECT remote_uid, msg_key FROM sync_msg_map WHERE account_email=%s AND folder=%s",
            (self.acc.email, folder))
        gone = [(int(u), k) for u, k in mapped if int(u) <= last_uid and int(u) not in present]
        guard = f"del:{folder}"
        if not gone:
            self.problem(guard, None)
            self.db.guard_clear(self.acc.email, guard)
            return
        if not present or len(gone) > REMOTE_DELETE_MAX_PER_PASS:
            # Could be a real bulk delete (phone/webmail) or a bad server
            # answer. Wait until it has stayed the same for a while (or the
            # admin approves) before acting on it.
            msg = (f"{folder}: {len(gone)} mails disappeared from Hostinger at once -- "
                   f"waiting {GUARD_CONFIRM_MINUTES} min to confirm"
                   + (" before moving the local copies to Trash" if DELETE_LOCAL_ON_REMOTE_DELETE else "")
                   + " (or press Approve)")
            if not self.db.guard_decide(self.acc.email, guard, msg, len(gone), GUARD_CONFIRM_MINUTES):
                # only worth a warning if local mail would be touched
                self.problem(guard, msg if DELETE_LOCAL_ON_REMOTE_DELETE else None)
                return
            log.info("%s %s: bulk delete of %d confirmed -- applying", self.tag, folder, len(gone))
        self.problem(guard, None)
        confirmed = set(self.confirm_gone(folder, [u for u, _k in gone]))
        gone = [(u, k) for u, k in gone if u in confirmed]
        if not gone:
            return
        removed = 0
        if DELETE_LOCAL_ON_REMOTE_DELETE:
            # per key: remove only as many local copies as Hostinger lost,
            # and never more than would leave fewer copies than Hostinger has
            gone_count = {}
            for _u, k in gone:
                if k:
                    gone_count[k] = gone_count.get(k, 0) + 1
            copies = self.local_copies(folder, list(gone_count))
            still = {}
            for k in gone_count:
                still[k] = len(uid_search(self.remote, "HEADER", "MESSAGE-ID", q(k))) if is_message_id(k) else 0
            luids = []
            for k, lst in copies.items():
                # local copies beyond what Hostinger still has, capped at how
                # many Hostinger actually lost
                n = min(max(0, len(lst) - still.get(k, 0)), gone_count[k])
                if n:
                    luids.extend(u for u, _f in lst[-n:])
            if luids:
                self.trash_local(folder, luids)   # recoverable in Roundcube's Trash
                removed = len(luids)
        for part in chunks([u for u, _k in gone], 500):
            self.db.execute(
                "DELETE FROM sync_msg_map WHERE account_email=%s AND folder=%s "
                f"AND remote_uid IN ({','.join(['%s'] * len(part))})",
                (self.acc.email, folder, *part))
        log.info("%s %s: %d message(s) were deleted on Hostinger -- %s", self.tag, folder, len(gone),
                 f"moved {removed} local copies to Trash" if DELETE_LOCAL_ON_REMOTE_DELETE
                 else "local copies kept (DELETE_LOCAL_ON_REMOTE_DELETE=0)")

    # ------------------------------------------------------------------
    #  Drafts (Hostinger side: phone creates / edits / sends / deletes)
    # ------------------------------------------------------------------
    def local_draft_copies(self, key):
        """{local_uid: content_hash} of local drafts with this key."""
        self.select_local(DRAFTS_FOLDER)
        if is_message_id(key):
            uids = uid_search(self.local, "HEADER", "MESSAGE-ID", q(key))
        else:
            uids = [r.uid for r in uid_fetch(self.local, "1:*", f"UID {HEADER_ITEM}")
                    if msg_key(r.data) == key]
        out = {}
        for part in chunks(uids, 20):
            for r in uid_fetch(self.local, uidset(part), "UID BODY.PEEK[]"):
                if isinstance(r.data, (bytes, bytearray)):
                    out[r.uid] = content_hash(r.data)
        return out

    def expunge_local(self, folder, luids):
        if not luids:
            return
        self.select_local(folder)
        check(*self.local.uid("STORE", uidset(luids), "+FLAGS.SILENT", "(\\Deleted)"), "local STORE")
        check(*self.local.uid("EXPUNGE", uidset(luids)), "local UID EXPUNGE")

    def trash_local(self, folder, luids):
        """Sync-driven local removal = MOVE to the local Trash (recoverable in
        Roundcube), never a hard delete. Only mail already in Trash is expunged."""
        if not luids:
            return
        if folder == TRASH_FOLDER:
            self.expunge_local(folder, luids)
            return
        try:
            self.local.create(q(TRASH_FOLDER))
        except Exception:
            pass  # usually exists already
        self.select_local(folder)
        check(*self.local.uid("MOVE", uidset(luids), q(TRASH_FOLDER)), f"local MOVE to {TRASH_FOLDER}")

    def confirm_gone(self, folder, uids):
        """Re-check on a refreshed session that these Hostinger UIDs are
        really gone. Returns only the ones confirmed missing."""
        self.remote.noop()
        self.examine_remote(folder)
        still = set(uid_search(self.remote, "UID", uidset(uids))) if uids else set()
        return [u for u in uids if u not in still]

    def pull_drafts(self, last_uid):
        """New UIDs in Hostinger's Drafts: a draft written or edited on
        another device. Put that version in the local Drafts and remove the
        older local copy -- unless you edited it locally meanwhile, then
        both are kept."""
        acc = self.acc.email
        self.examine_remote(DRAFTS_FOLDER)
        uids = [u for u in uid_search(self.remote, "UID", f"{last_uid + 1}:*") if u > last_uid]
        if not uids:
            return 0
        known = self.db.map_rows_by_uid(acc, DRAFTS_FOLDER, uids)
        states = self.db.draft_states(acc)
        copied = 0
        for part in chunks([u for u in uids if u not in known], BODY_BATCH):
            for r in uid_fetch(self.remote, uidset(part), "UID FLAGS INTERNALDATE BODY.PEEK[]"):
                if not isinstance(r.data, (bytes, bytearray)):
                    continue
                raw = bytes(r.data)
                key, h = msg_key(raw), content_hash(raw)
                st = states.get(key)
                local = self.local_draft_copies(key) if key else {}
                # older local version, unchanged since last sync -> replaced
                stale = [lu for lu, lh in local.items() if st and lh == st[1] and lh != h]
                if key:
                    # record BEFORE appending locally, so sync-push never
                    # mistakes this incoming version for a local edit.
                    # Same content as the state we know -> it's the copy
                    # sync-push just uploaded; keep its origin.
                    origin = st[2] if st and st[1] == h else "hostinger"
                    self.db.set_draft_state(acc, key, r.uid, h, origin)
                    states[key] = (r.uid, h, origin)
                if h not in local.values():
                    date = f'"{r.internaldate}"' if r.internaldate else None
                    check(*self.local.append(q(DRAFTS_FOLDER), appendable_flags(r.flags), date, raw),
                          "APPEND local draft")
                    copied += 1
                    self.expunge_local(DRAFTS_FOLDER, stale)
                if key:
                    self.db.upsert_map([(acc, DRAFTS_FOLDER, r.uid, key, flags_to_db(r.flags))])
        self.db.set_folder(acc, DRAFTS_FOLDER, remote_last_uid=max(uids))
        return copied

    def check_draft_vanish(self):
        """A draft's Hostinger copy disappeared and sync-push didn't remove
        it -> it was sent or deleted on another device. Remove the local
        copy too, unless it was edited locally since (then it's uploaded
        again as a new draft)."""
        acc = self.acc.email
        states = {k: v for k, v in self.db.draft_states(acc).items() if v[0] is not None}
        if not states:
            return
        if self.remote_selected == DRAFTS_FOLDER:
            self.remote.noop()
        self.examine_remote(DRAFTS_FOLDER)
        present = set(uid_search(self.remote, "ALL"))
        gone = {k: v for k, v in states.items() if v[0] not in present}
        if not gone:
            self.problem("drafts", None)
            self.db.guard_clear(self.acc.email, "pull-drafts")
            return
        if len(gone) > DRAFT_VANISH_LIMIT and (not present or len(gone) > len(states) // 2):
            msg = (f"{len(gone)} of {len(states)} drafts disappeared from Hostinger at once -- waiting "
                   f"{GUARD_CONFIRM_MINUTES} min to confirm before moving the Roundcube copies to Trash "
                   f"(or press Approve)")
            if not self.db.guard_decide(self.acc.email, "pull-drafts", msg, len(gone), GUARD_CONFIRM_MINUTES):
                self.problem("drafts", msg)
                return
            log.info("%s drafts: bulk removal of %d confirmed -- applying", self.tag, len(gone))
        self.problem("drafts", None)
        confirmed = set(self.confirm_gone(DRAFTS_FOLDER, [v[0] for v in gone.values()]))
        gone = {k: v for k, v in gone.items() if v[0] in confirmed}
        removed = kept = 0
        for key, (ruid, h, _origin) in gone.items():
            local = self.local_draft_copies(key)
            same = [lu for lu, lh in local.items() if lh == h]
            self.trash_local(DRAFTS_FOLDER, same)   # recoverable in Roundcube's Trash
            removed += len(same)
            if len(same) < len(local):
                kept += 1  # edited locally -> sync-push re-uploads it
                self.db.execute("UPDATE sync_draft_state SET remote_uid=NULL WHERE account_email=%s "
                                "AND msg_key=%s AND remote_uid=%s", (acc, key, ruid))
            else:
                self.db.execute("DELETE FROM sync_draft_state WHERE account_email=%s AND msg_key=%s "
                                "AND remote_uid=%s", (acc, key, ruid))
            self.db.delete_map_uids(acc, DRAFTS_FOLDER, [ruid])
        log.info("%s drafts removed on Hostinger: %d local cop(ies) moved to Trash, %d kept (edited locally)",
                 self.tag, removed, kept)

    # ------------------------------------------------------------------
    #  Local helpers
    # ------------------------------------------------------------------
    def local_lookup(self, folder, keys):
        """{key: (local_uid, local_flags)} for keys present in the local folder."""
        keys = [k for k in set(keys) if k]
        if not keys:
            return {}
        self.select_local(folder)
        if len(keys) > INDEX_THRESHOLD or any(not is_message_id(k) for k in keys):
            index = {}
            for r in uid_fetch(self.local, "1:*", f"UID FLAGS {HEADER_ITEM}"):
                k = msg_key(r.data)
                if k and k not in index:
                    index[k] = (r.uid, r.flags or frozenset())
            return {k: index[k] for k in keys if k in index}
        found = {}
        for k in keys:
            uids = uid_search(self.local, "HEADER", "MESSAGE-ID", q(k))
            if uids:
                found[k] = uids[0]
        if not found:
            return {}
        out = {}
        for r in uid_fetch(self.local, uidset(found.values()), f"UID FLAGS {HEADER_ITEM}"):
            k = msg_key(r.data)
            if k in found:
                out[k] = (r.uid, r.flags or frozenset())
        return out

    def local_copies(self, folder, keys):
        """{key: [(local_uid, flags), ...]} -- EVERY local copy of each key
        (a folder can legitimately hold the same Message-ID more than once)."""
        keys = [k for k in set(keys) if k]
        if not keys:
            return {}
        self.select_local(folder)
        out = {}
        if len(keys) > INDEX_THRESHOLD or any(not is_message_id(k) for k in keys):
            wanted = set(keys)
            for r in uid_fetch(self.local, "1:*", f"UID FLAGS {HEADER_ITEM}"):
                k = msg_key(r.data)
                if k in wanted:
                    out.setdefault(k, []).append((r.uid, r.flags or frozenset()))
        else:
            uids = {}
            for k in keys:
                for u in uid_search(self.local, "HEADER", "MESSAGE-ID", q(k)):
                    uids[u] = k
            if uids:
                for r in uid_fetch(self.local, uidset(uids), f"UID FLAGS {HEADER_ITEM}"):
                    k = msg_key(r.data)
                    if k == uids.get(r.uid):
                        out.setdefault(k, []).append((r.uid, r.flags or frozenset()))
        for k in out:
            out[k].sort()
        return out

    def mapped_counts(self, folder, keys):
        """How many Hostinger copies of each key are already linked to a
        local copy (so those local copies are 'taken')."""
        out = {}
        for part in chunks([k for k in set(keys) if k], 500):
            rows = self.db.fetchall(
                "SELECT msg_key, COUNT(*) FROM sync_msg_map WHERE account_email=%s AND folder=%s "
                f"AND msg_key IN ({','.join(['%s'] * len(part))}) GROUP BY msg_key",
                (self.acc.email, folder, *part))
            out.update({k: int(n) for k, n in rows})
        return out

    def set_local_flags(self, folder, luid, current, wanted):
        self.select_local(folder)
        cur, want = tracked(current), tracked(wanted)
        for flag in TRACKED_FLAGS:
            if flag in want and flag not in cur:
                check(*self.local.uid("STORE", str(luid), "+FLAGS.SILENT", f"({flag})"), "local STORE")
            elif flag in cur and flag not in want:
                check(*self.local.uid("STORE", str(luid), "-FLAGS.SILENT", f"({flag})"), "local STORE")

    # ------------------------------------------------------------------
    #  Folders (additive only)
    # ------------------------------------------------------------------
    def discover_folders(self):
        remote = list_folders(self.remote)
        local = list_folders(self.local)
        if remote is None or local is None:
            log.warning("%s could not LIST folders on one side -- skipped", self.tag)
            return
        known = self.db.folder_states(self.acc.email)
        acc = self.acc.email

        for folder in sorted(remote | local):
            on_remote, on_local, state = folder in remote, folder in local, known.get(folder)
            if on_remote and on_local:
                if state != "active":
                    self.db.set_folder(acc, folder, status="active")
            elif on_remote:  # only on Hostinger
                if state is None:
                    self.local.create(q(folder))
                    try:
                        self.local.subscribe(q(folder))
                    except Exception:
                        pass
                    self.db.set_folder(acc, folder, status="active")
                    log.info("%s new Hostinger folder '%s' -> created locally", self.tag, folder)
                elif state == "active":
                    # you deleted it in Roundcube: stop mirroring it, Hostinger keeps it
                    self.db.set_folder(acc, folder, status="local_deleted")
                    log.info("%s '%s' was deleted locally -- Hostinger copy kept, no longer mirrored",
                             self.tag, folder)
            else:  # only local
                if state is None:
                    check(*self.remote.create(q(folder)), f"CREATE {folder} on Hostinger")
                    try:
                        self.remote.subscribe(q(folder))
                    except Exception:
                        pass
                    self.db.set_folder(acc, folder, status="active")
                    log.info("%s new local folder '%s' -> created on Hostinger", self.tag, folder)
                elif state == "active":
                    self.db.set_folder(acc, folder, status="remote_deleted")
                    log.info("%s '%s' is gone from Hostinger -- local copy kept", self.tag, folder)


_IDLE_TRIGGERS = {b"EXISTS", b"RECENT", b"FETCH", b"EXPUNGE"}


def _has_changes(responses):
    for r in responses or []:
        if isinstance(r, (tuple, list)) and len(r) >= 2:
            token = r[1].encode() if isinstance(r[1], str) else r[1]
            if isinstance(token, bytes) and token.upper() in _IDLE_TRIGGERS:
                return True
    return False


def start_account(acc, stop):
    Puller(acc, stop).run()


if __name__ == "__main__":
    supervise("sync-pull", start_account, "pull")
