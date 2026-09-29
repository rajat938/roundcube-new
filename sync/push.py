"""
sync-push: local Dovecot -> Hostinger. Does nothing unless something
changed locally.

Every PUSH_POLL_SECONDS it asks the LOCAL Dovecot (same machine, ~1 ms)
for each folder's change counter (STATUS HIGHESTMODSEQ). If no counter
moved, it goes back to sleep -- no Hostinger traffic, no Hostinger login.
When a counter moved it fetches only what changed (CHANGEDSINCE) and:

  * read / unread / answered / flagged  -> same flag set on Hostinger
  * new message in Sent                 -> APPENDed to Hostinger's Sent
  * draft created / edited              -> uploaded to Hostinger's Drafts;
                                           the previous version is replaced
  * draft sent                          -> its copy leaves Hostinger's Drafts
  * draft deleted                       -> its copy MOVED to Hostinger's Trash
  * anything else (delete, move, empty trash, delete folder)
                                        -> NOT sent to Hostinger, ever

A Hostinger login is opened only when there is something to push and
closed again after REMOTE_IDLE_CLOSE_SECONDS without work. The
connection is a SafeRemoteIMAP, which refuses every delete/move command.
"""
import time

from common import (
    Backoff, Database, DRAFTS_FOLDER, SENT_FOLDER, TRASH_FOLDER, SyncError, HEADER_ITEM,
    _env, _env_bool, appendable_flags, appenduid, capabilities, check, chunks, upload_flags,
    content_hash, describe, enable_condstore,
    flags_to_db, is_message_id, list_folders, local_connect, log, msg_key, q,
    remote_connect, safe_logout, status, supervise, tracked, uid_fetch,
    uid_search, uidset,
)

PUSH_POLL_SECONDS = float(_env("PUSH_POLL_SECONDS", "3"))
LOCAL_FOLDER_REFRESH_SECONDS = int(_env("LOCAL_FOLDER_REFRESH_SECONDS", "60"))
REMOTE_IDLE_CLOSE_SECONDS = int(_env("REMOTE_IDLE_CLOSE_SECONDS", "600"))
SENT_CATCHUP_DAYS = int(_env("SENT_CATCHUP_DAYS", "7"))  # first start only

SYNC_DRAFTS = _env_bool("SYNC_DRAFTS", True)
# What happens to the OLD Hostinger copy when a draft is edited or sent:
#   remove -> the old copy is removed, but ONLY if sync-push uploaded that
#             copy itself (it is just an older version of your own draft);
#             a copy that came from Hostinger (e.g. written on the phone)
#             is always moved to Trash instead.
#   trash  -> every old copy is moved to Hostinger's Trash.
# A draft you DELETE in Roundcube is always moved to Hostinger's Trash.
DRAFT_OLD_VERSIONS = _env("DRAFT_OLD_VERSIONS", "trash").strip().lower()
# Safety net: if more drafts than this look "deleted in Roundcube" in one
# pass (e.g. the local mail folder came up empty after a restore or a
# wrong volume path), do NOTHING on Hostinger and log a warning instead.
DRAFT_MASS_DELETE_LIMIT = int(_env("DRAFT_MASS_DELETE_LIMIT", "3"))


class Pusher:
    def __init__(self, acc, stop):
        self.acc, self.stop = acc, stop
        self.tag = f"[push {acc.email}]"
        self.db = Database()
        self.local = None
        self.local_selected = None
        self.remote = None
        self.remote_last_used = 0
        self.remote_selected = None
        self.folders = []
        self.folders_at = 0
        self._status = (None, None)
        self._status_at = 0
        self.drafts_problem = None

    def status(self, state, detail=""):
        now = time.time()
        if (state, detail) == self._status and now - self._status_at < 30:
            return
        try:
            self.db.set_status(self.acc.email, "push", state, detail)
            self._status, self._status_at = (state, detail), now
        except Exception:
            pass

    # ------------------------------------------------------------------
    def run(self):
        backoff = Backoff()
        while not self.stop.is_set():
            try:
                self.local = local_connect(self.acc)
                self.local_selected = None
                if not enable_condstore(self.local):
                    raise SyncError("local Dovecot does not offer CONDSTORE")
                backoff.reset()
                self.loop()
            except Exception as e:
                delay = backoff.next(e)
                log.warning("%s %s -- retrying in %ss", self.tag, describe(e), delay)
                self.status("error", f"{describe(e)} -- retrying in {delay}s")
                self.stop.wait(delay)
            finally:
                safe_logout(self.local)
                self.local = None
                self.close_remote()
        self._status = (None, None)
        self.status("stopped", getattr(self.stop, "reason", "stopped"))
        self.db.close()
        log.info("%s stopped", self.tag)

    def loop(self):
        while not self.stop.is_set():
            if time.time() - self.folders_at > LOCAL_FOLDER_REFRESH_SECONDS:
                listed = list_folders(self.local)
                if listed is not None:
                    self.folders = sorted(listed)
                    self.folders_at = time.time()
            for folder in self.folders:
                if self.stop.is_set():
                    return
                self.check_folder(folder)
            if self.remote and time.time() - self.remote_last_used > REMOTE_IDLE_CLOSE_SECONDS:
                self.close_remote()
            if self.drafts_problem:
                self.status("error", self.drafts_problem)
            else:
                self.status("up to date", "watching Roundcube for changes")
            self.stop.wait(PUSH_POLL_SECONDS)

    # ------------------------------------------------------------------
    #  Hostinger connection (opened only when needed)
    # ------------------------------------------------------------------
    def ensure_remote(self):
        if self.remote is not None:
            try:
                if time.time() - self.remote_last_used > 120:
                    self.remote.noop()
            except Exception:
                self.close_remote()
        if self.remote is None:
            self.remote = remote_connect(self.acc)
            self.remote_selected = None
        self.remote_last_used = time.time()
        return self.remote

    def close_remote(self):
        safe_logout(self.remote)
        self.remote = None
        self.remote_selected = None

    def select_remote(self, folder, readonly):
        mode = (folder, readonly)
        if self.remote_selected != mode:
            self.remote_selected = None
            check(*self.remote.select(q(folder), readonly=readonly), f"SELECT {folder} on Hostinger")
            self.remote_selected = mode

    # ------------------------------------------------------------------
    def examine_local(self, folder):
        check(*self.local.select(q(folder), readonly=True), f"EXAMINE local {folder}")
        self.local_selected = folder

    def check_folder(self, folder):
        if self.local_selected == folder:
            # STATUS on the open folder returns a stale view until NOOP
            self.local.noop()
        st = status(self.local, folder, "UIDNEXT UIDVALIDITY HIGHESTMODSEQ")
        if st is None or "HIGHESTMODSEQ" not in st:
            return
        state = self.db.folder_state(self.acc.email, folder) or {}
        if state.get("local_uidvalidity") != st["UIDVALIDITY"] or state.get("local_modseq") is None:
            # First time we see this folder (or it was recreated): take a
            # bookmark, push nothing old. Sent gets a short catch-up;
            # Drafts gets a full reconcile (drafts are few).
            if folder == SENT_FOLDER:
                self.catch_up_sent()
            elif folder == DRAFTS_FOLDER and SYNC_DRAFTS:
                if not self.sync_drafts():
                    return
            self.save(folder, st)
            return
        if st["HIGHESTMODSEQ"] == state["local_modseq"]:
            return  # nothing changed locally -> nothing to do

        if folder == DRAFTS_FOLDER and SYNC_DRAFTS:
            if self.sync_drafts():
                self.save(folder, st)   # not saved while blocked -> re-checked every poll
            return
        if folder == SENT_FOLDER and st["UIDNEXT"] > (state.get("local_uidnext") or 0):
            self.push_sent_since(int(state.get("local_uidnext") or 1))
        self.push_flags(folder, int(state["local_modseq"]))
        self.save(folder, st)

    def save(self, folder, st):
        self.db.set_folder(self.acc.email, folder, local_uidvalidity=st["UIDVALIDITY"],
                           local_uidnext=st["UIDNEXT"], local_modseq=st["HIGHESTMODSEQ"])

    # ------------------------------------------------------------------
    #  Flags
    # ------------------------------------------------------------------
    def push_flags(self, folder, since):
        self.examine_local(folder)
        recs = uid_fetch(self.local, "1:*", f"UID FLAGS {HEADER_ITEM}", changedsince=since)
        if not recs:
            return
        for r in recs:
            r.key = msg_key(r.data)
        mapped = self.db.map_rows_by_key(self.acc.email, folder, [r.key for r in recs])

        ops = {}        # (op, flag) -> [remote uids]
        new_base = []   # (remote_uid, flags)
        for r in recs:
            if r.flags is None or r.key not in mapped:
                continue  # message not on Hostinger in this folder (moved/new locally) -> nothing to push
            want = tracked(r.flags)
            for ruid, base in mapped[r.key]:
                if want == base:
                    continue
                for flag in want - base:
                    ops.setdefault(("+FLAGS.SILENT", flag), []).append(ruid)
                for flag in base - want:
                    ops.setdefault(("-FLAGS.SILENT", flag), []).append(ruid)
                new_base.append((ruid, want))
        if not ops:
            return

        self.ensure_remote()
        self.select_remote(folder, readonly=False)
        for (op, flag), uids in ops.items():
            for part in chunks(uids, 500):
                check(*self.remote.uid("STORE", uidset(part), op, f"({flag})"),
                      f"STORE {flag} on Hostinger")
        for ruid, want in new_base:
            self.db.set_baseline(self.acc.email, folder, [ruid], want)
        log.info("%s %s: pushed %d flag change(s) on %d message(s) to Hostinger",
                 self.tag, folder, sum(len(u) for u in ops.values()), len(new_base))

    # ------------------------------------------------------------------
    #  Sent mail (additive: APPEND only)
    # ------------------------------------------------------------------
    def push_sent_since(self, from_uid):
        self.examine_local(SENT_FOLDER)
        uids = [u for u in uid_search(self.local, "UID", f"{from_uid}:*") if u >= from_uid]
        self.upload_sent(uids)

    def catch_up_sent(self):
        self.examine_local(SENT_FOLDER)
        since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - SENT_CATCHUP_DAYS * 86400))
        self.upload_sent(uid_search(self.local, "SINCE", since))

    def upload_sent(self, uids):
        if not uids:
            return
        uploaded = 0
        for part in chunks(uids, 20):
            self.examine_local(SENT_FOLDER)
            for r in uid_fetch(self.local, uidset(part), "UID FLAGS INTERNALDATE BODY.PEEK[]"):
                if not isinstance(r.data, (bytes, bytearray)):
                    continue
                key = msg_key(r.data)
                if not key:
                    log.warning("%s Sent uid %s has no usable identity -- not uploaded", self.tag, r.uid)
                    continue
                if self.already_on_hostinger(key):
                    continue
                self.ensure_remote()
                if is_message_id(key):
                    self.select_remote(SENT_FOLDER, readonly=True)
                    if uid_search(self.remote, "HEADER", "MESSAGE-ID", q(key)):
                        self.mark_uploaded(key)
                        continue
                flags = set(r.flags or ()) | {"\\Seen"}
                date = f'"{r.internaldate}"' if r.internaldate else None
                typ, data = self.remote.append(q(SENT_FOLDER), upload_flags(flags), date, bytes(r.data))
                check(typ, data, "APPEND to Hostinger Sent")
                self.remote_selected = None
                ruid = appenduid(data)
                if ruid:
                    self.db.upsert_map([(self.acc.email, SENT_FOLDER, ruid, key, flags_to_db(flags))])
                self.mark_uploaded(key)
                uploaded += 1
        if uploaded:
            log.info("%s uploaded %d sent message(s) to Hostinger", self.tag, uploaded)

    # ------------------------------------------------------------------
    #  Drafts (two-way; old versions replaced, deletes go to Trash)
    # ------------------------------------------------------------------
    def local_drafts(self):
        """{key: (uid, hash)} for the newest local copy of each draft."""
        self.examine_local(DRAFTS_FOLDER)
        heads = uid_fetch(self.local, "1:*", f"UID {HEADER_ITEM}")
        newest = {}
        for r in heads:
            k = msg_key(r.data)
            if k and (k not in newest or r.uid > newest[k]):
                newest[k] = r.uid
        out = {}
        by_uid = {u: k for k, u in newest.items()}
        for part in chunks(sorted(by_uid), 20):
            for r in uid_fetch(self.local, uidset(part), "UID BODY.PEEK[]"):
                if r.uid in by_uid and isinstance(r.data, (bytes, bytearray)):
                    out[by_uid[r.uid]] = (r.uid, content_hash(r.data))
        return out

    def remote_drafts_with_key(self, key):
        """{remote_uid: hash} of Hostinger drafts carrying this Message-ID."""
        if not is_message_id(key):
            return {}
        self.ensure_remote()
        self.select_remote(DRAFTS_FOLDER, readonly=False)
        uids = uid_search(self.remote, "HEADER", "MESSAGE-ID", q(key))
        out = {}
        for part in chunks(uids, 20):
            for r in uid_fetch(self.remote, uidset(part), "UID BODY.PEEK[]"):
                if isinstance(r.data, (bytes, bytearray)):
                    out[r.uid] = content_hash(r.data)
        return out

    def in_local_sent(self, key):
        if not is_message_id(key):
            return False
        self.examine_local(SENT_FOLDER)
        return bool(uid_search(self.local, "HEADER", "MESSAGE-ID", q(key)))

    def retire_remote_draft(self, ruid, origin, reason):
        """Take an old copy out of Hostinger's Drafts. Removed only if we
        uploaded it ourselves and it's being replaced/sent; otherwise moved
        to Trash (recoverable)."""
        self.ensure_remote()
        self.select_remote(DRAFTS_FOLDER, readonly=False)
        if not uid_search(self.remote, "UID", str(ruid)):
            return "already gone"
        caps = capabilities(self.remote)
        remove = (reason in ("replaced", "sent") and origin == "sync" and DRAFT_OLD_VERSIONS == "remove")
        uid = str(ruid)
        with self.remote.permit_draft_cleanup([ruid]):
            if not remove and "MOVE" in caps:
                check(*self.remote.uid("MOVE", uid, TRASH_FOLDER), "MOVE draft to Trash")
                return "moved to Trash"
            if "UIDPLUS" not in caps:
                log.warning("%s Hostinger lacks UIDPLUS -- old draft copy uid %s left in place", self.tag, ruid)
                return "left (no UIDPLUS)"
            if not remove:
                check(*self.remote.uid("COPY", uid, TRASH_FOLDER), "COPY draft to Trash")
            check(*self.remote.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"), "flag old draft")
            check(*self.remote.uid("EXPUNGE", uid), "UID EXPUNGE old draft")
            return "removed" if remove else "moved to Trash"

    def upload_draft(self, luid):
        self.examine_local(DRAFTS_FOLDER)
        recs = uid_fetch(self.local, str(luid), "UID FLAGS INTERNALDATE BODY.PEEK[]")
        if not recs or not isinstance(recs[0].data, (bytes, bytearray)):
            return None
        r = recs[0]
        self.ensure_remote()
        flags = set(r.flags or ()) | {"\\Draft", "\\Seen"}
        date = f'"{r.internaldate}"' if r.internaldate else None
        typ, data = self.remote.append(q(DRAFTS_FOLDER), upload_flags(flags), date, bytes(r.data))
        check(typ, data, "APPEND to Hostinger Drafts")
        self.remote_selected = None
        ruid = appenduid(data)
        key = msg_key(r.data)
        if ruid is None and is_message_id(key):
            self.select_remote(DRAFTS_FOLDER, readonly=False)
            found = uid_search(self.remote, "HEADER", "MESSAGE-ID", q(key))
            ruid = max(found) if found else None
        if ruid is not None:
            self.db.upsert_map([(self.acc.email, DRAFTS_FOLDER, ruid, key, flags_to_db(flags))])
        return ruid

    def sync_drafts(self):
        acc = self.acc.email
        local = self.local_drafts()
        states = self.db.draft_states(acc)
        uploaded = replaced = sent = deleted = 0

        for key, (luid, h) in local.items():
            st = states.get(key)
            if st and st[1] == h and st[0] is not None:
                continue  # unchanged
            old = []  # (remote_uid, origin)
            if st and st[0] is not None:
                old.append((st[0], st[2]))
            elif not st:
                # never seen: maybe Hostinger already has it (first start,
                # or pull hasn't recorded it yet)
                remote = self.remote_drafts_with_key(key)
                same = [u for u, rh in remote.items() if rh == h]
                if same:
                    self.db.set_draft_state(acc, key, max(same), h, "hostinger")
                    continue
                old.extend((u, "hostinger") for u in remote)
            ruid = self.upload_draft(luid)
            if ruid is None:
                log.warning("%s draft %s uploaded but its Hostinger UID is unknown", self.tag, key)
            self.db.set_draft_state(acc, key, ruid, h, "sync")
            for ouid, origin in old:
                if ouid != ruid:
                    self.retire_remote_draft(ouid, origin, "replaced")
                    self.db.delete_map_uids(acc, DRAFTS_FOLDER, [ouid])
            if old:
                replaced += 1
            else:
                uploaded += 1

        # drafts that disappeared locally: sent (-> leave Drafts) or deleted
        # (-> Hostinger Trash).
        vanished = [k for k, v in states.items() if k not in local and v[0] is not None]
        sent_keys = {k for k in vanished if self.in_local_sent(k)}
        not_sent = [k for k in vanished if k not in sent_keys]
        tracked_remote = sum(1 for v in states.values() if v[0] is not None)
        hold_deletes = False
        if (len(not_sent) > DRAFT_MASS_DELETE_LIMIT
                or (not local and tracked_remote >= 2 and not_sent)):
            # Many drafts gone from Roundcube at once: a real clean-up, or a
            # local mail folder that came up empty (restore, wrong volume).
            # Moving Hostinger's drafts to Trash waits for the admin's OK --
            # everything else (new/edited/sent drafts) keeps syncing.
            msg = (f"{len(not_sent)} drafts disappeared from Roundcube at once -- NOT moved to "
                   f"Hostinger's Trash yet. If you deleted them, press Approve; if not, check the "
                   f"local mail folder.")
            if self.db.guard_decide(acc, "push-drafts", msg, len(not_sent), None):
                log.info("%s drafts: bulk delete of %d approved -- applying", self.tag, len(not_sent))
            else:
                hold_deletes = True
                if msg != self.drafts_problem:
                    log.warning("%s %s", self.tag, msg)
                self.drafts_problem = msg
        else:
            self.db.guard_clear(acc, "push-drafts")
        if not hold_deletes:
            self.drafts_problem = None

        for key, (ruid, _h, origin) in states.items():
            if key in local:
                continue
            if ruid is None:
                self.db.delete_draft_state(acc, key)
                continue
            reason = "sent" if key in sent_keys else "deleted"
            if reason == "deleted" and hold_deletes:
                continue          # held until approved
            self.retire_remote_draft(ruid, origin, reason)
            self.db.delete_map_uids(acc, DRAFTS_FOLDER, [ruid])
            self.db.delete_draft_state(acc, key)
            if reason == "sent":
                sent += 1
            else:
                deleted += 1

        if uploaded or replaced or sent or deleted:
            log.info("%s drafts -> Hostinger: %d new, %d updated, %d sent (left Drafts), "
                     "%d deleted (moved to Trash)", self.tag, uploaded, replaced, sent, deleted)
        return not hold_deletes   # while held: folder re-checked every poll

    def already_on_hostinger(self, key):
        if self.db.fetchone("SELECT 1 FROM sent_upload_state WHERE account_email=%s AND message_id=%s",
                            (self.acc.email, key)):
            return True
        return bool(self.db.fetchone(
            "SELECT 1 FROM sync_msg_map WHERE account_email=%s AND folder=%s AND msg_key=%s LIMIT 1",
            (self.acc.email, SENT_FOLDER, key)))

    def mark_uploaded(self, key):
        self.db.execute("INSERT IGNORE INTO sent_upload_state (account_email, message_id, remote_verified) "
                        "VALUES (%s,%s,1)", (self.acc.email, key))


def start_account(acc, stop):
    Pusher(acc, stop).run()


if __name__ == "__main__":
    supervise("sync-push", start_account, "push")
