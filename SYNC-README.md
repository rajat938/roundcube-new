# Apply this update to your repo (do this first)

```bash
cd ~/project/RoundCube_Mail-main
git pull                                   # get what's on GitHub
unzip -o ~/roundcube-repo-update.zip -d .  # new/updated files on top
bash tools/upgrade_repo.sh                 # fixes .env, scrubs config, untracks secrets, deletes leftovers
```
The script stops once to ask for `MYSQL_ROOT_PASSWORD` if it isn't in `.env` yet.
It keeps your current `des_key`, so nobody is logged out. Then:
```bash
git add -A && git status                   # .env / config files must show as deleted (= untracked), not modified
git commit -m "Security cleanup: secrets out of git, current config"
git push                                   # the Deploy workflow now pulls + restarts on the server
```
**Also do by hand:**
- make the GitHub repo **private**;
- change the DB and root passwords;
- run `app.py setup` again (new admin password + `SECRET_KEY`);
- clean the git history (`git filter-repo`) or start a fresh repo.

The old values are in git history, and the repo was public.

# ⚠ Update: review fixes (read this first)

## Do these yourself (I can't do them for you)
1. **Make the GitHub repo private** (Settings → General → Danger zone → Change visibility).
2. **Change every secret that was ever committed**: MariaDB root + roundcube passwords, the
   admin password (`setup` again), `SECRET_KEY`, and -- only if the repo was public --
   Roundcube's `des_key` (changing it signs everyone out and ident_switch identities must
   re-enter their passwords). To change the DB password of the existing database:
   `docker exec -it roundcube-db mariadb -uroot -p -e "ALTER USER 'roundcube'@'%' IDENTIFIED BY 'NEW'; ALTER USER 'root'@'%' IDENTIFIED BY 'NEWROOT'; ALTER USER 'root'@'localhost' IDENTIFIED BY 'NEWROOT';"`
   then put the new values in `.env`.
3. **Remove secrets from git history**: `pip install git-filter-repo`, then
   `git filter-repo --invert-paths --path .env --path www/config/config.old.php --path www/config/config.docker.inc.php`
   and force-push -- or simply start a fresh repo from the cleaned files.

## Safety guards: what happens when a lot disappears at once

Big bulk deletes are *paused and shown*, never silently applied and never stuck:

| Situation | What the sync does | How it continues |
|---|---|---|
| Many mails deleted on Hostinger (phone/webmail), `DELETE_LOCAL_ON_REMOTE_DELETE=0` | only updates its own records; nothing local changes | automatic, no warning |
| Same, with `DELETE_LOCAL_ON_REMOTE_DELETE=1` (> 20 at once) | holds, shows **Waiting for you** on the account | goes ahead by itself after `GUARD_CONFIRM_MINUTES` (15) if still true, or press **Approve**; local copies go to Roundcube's Trash |
| More than 3 drafts (and over half) deleted on the phone | holds, shows it | same: auto after 15 min, or **Approve**; Roundcube copies go to Trash |
| More than 3 drafts deleted in Roundcube at once | holds moving their Hostinger copies to Trash; new/edited/sent drafts keep syncing | only **Approve** (it could be an empty local mail folder) |
| Small numbers (≤ 3 drafts, ≤ 20 mails) | applied right away | -- |

A glitch (e.g. Hostinger briefly answering with an empty folder) disappears by itself and the
hold clears; a real bulk delete stays the same and is applied after the wait. When a situation
changes (more deletes), the wait restarts.

## Install this version (manual steps -- tools/upgrade_repo.sh does these for you)
```bash
# 1. files: replace sync/, account-manager/, caddy/, dovecot/, roundcube/Dockerfile,
#    docker-compose.yml, .gitignore; add sync.env and tools/
# 2. secrets: make .env from .env.example (secrets only) -- copy your current DB password,
#    root password, ADMIN_PASSWORD_HASH, SECRET_KEY, and des_key into it
#    settings: sync.env holds all sync settings -- copy over any you changed
#    (e.g. DELETE_LOCAL_ON_REMOTE_DELETE=1)
# 3. remove secrets from Roundcube's config (prints your des_key -- put it in .env first):
python3 tools/scrub_roundcube_config.py www/config/config.inc.php
# 4. clean up old files
rm -f dovecot/conf/dovecot-sql.conf.ext dovecot/conf/users www/config/config.old.php docker q "#Uf03aq"
rm -rf nginx monitor sync-daemon
docker compose rm -sf monitor 2>/dev/null
# 5. rebuild + start
docker compose up -d --build --remove-orphans   # also stops old monitor/sync-daemon containers
```
Webmail is now **https://your-server:8444** (plain http :8081 only works on the server itself).
Admin page stays https://your-server:8443.

## What changed
| Review point | Fix |
|---|---|
| `\Deleted` could be uploaded to Hostinger | uploads only carry `\Seen \Answered \Flagged \Draft` (tested) |
| Empty/restored local Drafts could send all Hostinger drafts to Trash | > `DRAFT_MASS_DELETE_LIMIT` (3) at once waits for **Approve** in the admin page; everything else keeps syncing (tested) |
| `DRAFT_OLD_VERSIONS=remove` permanently deletes | default is now `trash` -- nothing is ever permanently deleted on Hostinger |
| IDLE connection bypassed the guard | now has its own allow-list: login / read-only / IDLE only (tested) |
| Draft-vanish guard too weak | holds > 3 (and > half), re-checks, confirms after 15 min or Approve, local copy goes to **Trash** (tested) |
| Remote-delete limit too loose | holds > `REMOTE_DELETE_MAX_PER_PASS` (20), confirms after 15 min or Approve, local copies go to **Trash**; with local deletes off it only updates bookkeeping (tested) |
| Guards could get stuck forever | every hold now resolves: auto-confirm or Approve button, status returns to normal (tested) |
| Same Message-ID mails missed | already fixed in the previous version (every copy downloaded) |
| Secrets in git | compose/dovecot/Roundcube read them from `.env`; each container gets only its own secrets |
| Roundcube `tls://dovecot` | now `dovecot` (worked before, but only because STARTTLS was silently skipped) |
| Roundcube on plain HTTP | HTTPS on :8444 via Caddy, :8081 bound to 127.0.0.1 |
| SMTP/IMAP certificate checks off | removed by `tools/scrub_roundcube_config.py` |
| `seccomp=unconfined` | replaced by `--innodb-use-native-aio=0` (same fix, sandbox kept) |
| `latest` image | pinned to `roundcube/roundcubemail:1.7.4-apache` |
| monitor, nginx, stray files | removed |

# New mail sync: sync-pull + sync-push

Replaces the old `sync-daemon` (imapsync + `idle_daemon.py`). It works the
way Thunderbird does:

- **New mail:** it asks Hostinger only for UIDs above the last one it saw.
- **Changes:** it asks only for what changed since the last check (CONDSTORE).
- **Logins:** a few logins per account, kept open.

## What goes where

| You do this in Roundcube            | Effect on Hostinger              |
|-------------------------------------|----------------------------------|
| receive mail                        | nothing (pulled down in ~1 s)    |
| read / unread / flag / answer       | same flag set (~1–3 s)           |
| send mail                           | copy **added** to Sent (~3 s)    |
| create folder                       | folder **added**                 |
| delete, move, empty Trash           | **nothing**, Hostinger keeps it  |
| delete folder                       | **nothing**                      |
| save / edit a draft                 | draft updated in Drafts (~2–3 s) |
| send a draft                        | draft leaves Drafts; mail in Sent|
| delete a draft                      | draft **moved to Trash** (recoverable) |

| Happens on Hostinger (phone etc.)   | Effect on local mirror           |
|-------------------------------------|----------------------------------|
| new mail                            | copied in ~1 s (IDLE)            |
| read / unread / flag                | applied in ~1 s                  |
| new folder                          | created locally                  |
| message deleted                     | local copy kept (or removed if `DELETE_LOCAL_ON_REMOTE_DELETE=1`) |
| folder deleted                      | local folder kept                |
| draft written / edited              | shows in Roundcube (≤ ~1 min)    |
| draft sent / deleted                | removed from Roundcube Drafts (≤ ~1 min) |

**Hostinger safety.** Every Hostinger connection goes through
`SafeRemoteIMAP` in `common.py`. It refuses the following before anything is sent:
- EXPUNGE, CLOSE, DELETE, RENAME, MOVE and COPY;
- plain STORE;
- any flag change other than `\Seen`, `\Answered` and `\Flagged`.

There is **one narrow exception, for drafts only**. The draft code can register specific
Drafts UIDs, and only those UIDs can be moved to Trash, or removed when the copy is an
old version that sync-push uploaded itself. This works only inside the Drafts folder.
Anything else stays blocked, including other folders, other UIDs, other destinations,
plain EXPUNGE and `1:*` ranges. The tests check this.

## Security: what goes to Hostinger and how

| Connection | Protection | Verified |
|---|---|---|
| sync-pull and sync-push to Hostinger IMAP (993) | TLS 1.2+, **certificate and hostname checked** | An impostor server was refused before the password was sent |
| IDLE watcher to Hostinger | Same TLS settings | Same test |
| Which server gets the password | Only hosts in `REMOTE_ALLOWED_HOSTS` (default `imap.hostinger.com`) | Tested: `evil.example.com` refused |
| Commands sent to Hostinger | Allow-list: no delete, move or expunge, except replacing old draft copies (Drafts only) | 19/19 tests |
| Folder names and Message-IDs in commands | Quoted; line breaks refused, so no command smuggling | Tested |
| Sync to local Dovecot and MariaDB | Plain, but only inside the Docker network, with no ports published | — |
| Container | Runs as `nobody`, pinned library versions | — |

The following are **outside the sync code but still need fixing**. See the notes that came with this zip.
- Roundcube → Hostinger SMTP has certificate checks turned off in `config.inc.php`.
- ~~Account manager without login over plain HTTP~~: fixed. See "Account manager login" below.
- Hostinger passwords are stored in plain text in `mirror_accounts`, because the sync needs them to log in. Protect the database and its password.

## How sent mail is saved

1. You click **Send**, and Roundcube sends the mail through **Hostinger SMTP**
   (`ssl://smtp.hostinger.com:465`). The recipient gets it immediately.
2. Roundcube saves a copy in the **local** `INBOX.Sent` (the mirror).
3. Within about 3 s, sync-push sees the local Sent folder change. It checks whether Hostinger's
   Sent already has that Message-ID, which prevents duplicates if Hostinger ever saves one itself.
   If it doesn't, sync-push **adds** the copy to Hostinger's `INBOX.Sent`, marked as read.
4. It records this in `sent_upload_state`, so the mail is never uploaded twice. If you later
   delete it on Hostinger, it won't come back either.
5. sync-pull sees the new mail in Hostinger's Sent, recognises it as the one push uploaded,
   and doesn't copy it back, so there's no local duplicate.

If Hostinger is unreachable at step 3, it retries automatically. The local copy is never
removed after upload.

## How drafts sync

Roundcube saves a draft as a new copy and deletes the previous one. The Message-ID stays
the same on every save and on the final send. That's how the sync tells these cases apart:

| Case | What happens |
|---|---|
| New draft / autosave / edit in Roundcube | New version uploaded to Hostinger Drafts. The old Hostinger copy is **removed if the sync uploaded it**, or **moved to Trash** if it came from the phone. |
| Draft sent from Roundcube | Found in local Sent under the same Message-ID, so the draft copy leaves Hostinger Drafts. The sent mail goes to Sent as described above. |
| Draft deleted in Roundcube | Hostinger copy **moved to Trash** (recoverable) |
| Draft written/edited on phone | New version copied into Roundcube, and the old local copy replaced |
| Draft sent/deleted on phone | Local copy removed, **unless you edited it in Roundcube meanwhile**. Then your edit is kept and uploaded as a draft again. |
| Same draft edited on both sides at once | Both versions kept locally, nothing lost |

Set `DRAFT_OLD_VERSIONS=trash` if you want every old draft version kept in Hostinger's Trash,
or `SYNC_DRAFTS=0` to keep drafts local only.

## Install

1. Copy the `sync/` folder into the project root, next to `docker-compose.yml`.
2. Replace `docker-compose.yml` with the new one. Only the `sync-daemon` block changed: it becomes `sync-pull` and `sync-push`.
3. Create `.env` from `.env.example` and put in your current DB password. Add `.env` to `.gitignore`.
4. Switch over:
   ```bash
   docker compose stop sync-daemon && docker compose rm -f sync-daemon
   docker compose up -d --build sync-pull sync-push
   docker logs -f mail-sync-pull
   ```
   Expected output: `connected (Hostinger CONDSTORE: yes)`. If it says `no`,
   everything still works, but read/unread changes made on the phone show up
   within `FLAG_RESCAN_SECONDS` instead of about 1 s.

**First start.** The first run matches the mail already in the mirror by Message-ID. It doesn't copy that mail again, and it copies only messages that exist on Hostinger but are missing locally, the same way the old imapsync did. In testing, 5,700 messages were matched in 6 s. Real Hostinger will take a bit longer. After the first run, local deletes and moves stay done.

**Rollback.**
```bash
docker compose stop sync-pull sync-push
```
Then put the old `sync-daemon` block back and run `docker compose up -d sync-daemon`.

New tables: `sync_folder_state`, `sync_msg_map`, `sync_draft_state`. Old tables from the previous
daemon (`remote_seen_state`, `move_sync_state`, `drafts_upload_state`,
`draft_seen_local_state`, `tracked_folders`) are no longer used and can be
dropped once you're happy.

## Useful log lines

```
[pull a@x.com] INBOX: 1 new message(s) copied in 0.4s (new mail)
[pull a@x.com] INBOX: applied 1 flag change(s) from Hostinger
[push a@x.com] INBOX: pushed 1 flag change(s) on 1 message(s) to Hostinger
[push a@x.com] uploaded 1 sent message(s) to Hostinger
```

## Also recommended

- In `www/config/config.inc.php`, set `$config['refresh_interval'] = 15;` (it's 30 now) so
  Roundcube shows new mail sooner. Users who changed "Check for new messages"
  in their own Settings keep their value.
- The earlier review's security items still apply:
  - put a login in front of `account-manager` on port 5000, or bind it to `127.0.0.1`;
  - move the other hardcoded passwords into `.env`;
  - turn certificate checks back on in `config.inc.php`.


## Account manager login (HTTPS)

The account manager now has a web login and is only reachable over HTTPS, through the
`caddy` service. It is no longer published on port 5000.

**Setup (once):**
```bash
docker compose build account-manager
docker compose run --rm account-manager python3 /app/app.py setup
#   -> type an admin password (12+ characters) twice
#   -> it prints ADMIN_PASSWORD_HASH='...' and SECRET_KEY='...'
#   -> paste both lines into .env, WITH the single quotes
docker compose up -d account-manager caddy
```
Open **https://your-server-ip:8443** and sign in as `admin` (or your `ADMIN_USERNAME`).
Open port 8443 in your server firewall if needed.

**The certificate warning:** by default Caddy makes its own certificate, so the browser shows
a warning the first time. The connection is still fully encrypted. To get a normal
certificate with no warning:
- point a domain such as `admin.yourdomain.com` at the server;
- set `ADMIN_SITE=admin.yourdomain.com` in `.env`;
- mount `caddy/Caddyfile.domain` instead of `caddy/Caddyfile`;
- publish ports `80` and `443` instead of `8443`.

Caddy then gets and renews a Let's Encrypt certificate by itself.

**Protections:**
- **Password storage:** the admin password is stored only as a scrypt hash.
- **Guessing:** 5 wrong passwords lock that IP for 15 minutes, and 30 in total pause all logins.
  A wrong username takes exactly as long as a wrong password.
- **Session cookie:** `__Host-` prefix, Secure, HttpOnly and SameSite=Strict. You get a new
  session at every login, are signed out after 30 min idle, and after 12 h at most.
- **Changes:** every change is a POST with a CSRF token, so a link someone sends you can't pause an account.
- **Hostinger passwords:** checked against Hostinger over verified TLS **before** saving, so a typo
  can't cause failed logins or a lock-out.
- **Roundcube passwords:** shown once when created or reset, never listed again. There's a
  "New Roundcube password" button if one is lost.
- **Page security:** strict headers (CSP, no framing, HSTS, no-store), no JavaScript, and no secrets in cookies or logs.
- **Audit log:** `docker logs account-manager` shows logins, failed attempts and every change,
  with the user and IP.

Change the admin password by running `setup` again and replacing `ADMIN_PASSWORD_HASH`.
Keep `SECRET_KEY` unless you want to sign everyone out.


## Admin page: progress and start/stop

The admin page (https://your-server:8443) shows every account as a card:

- **Status:** Up to date / Syncing / Connecting / Problem / Stopped / Not responding, with a
  one-line detail, for example `INBOX: 1,600 of 2,500 left`.
- **Progress bar and numbers:** mail on Hostinger, mail in Roundcube, mail waiting to download,
  and an estimate of the time left while a big first download is running.
- **Folders:** the same numbers for each folder.
- **Sending/read-status sync:** whether sync-push is working for that account.

Use **Auto-refresh every 10s** at the top to watch a download live. The "Add account" form is
hidden while auto-refresh is on, so a refresh can't wipe what you're typing.

**Controls:**
- **Stop sync / Start sync** on an account stops or starts just that account. A running download
  stops within seconds and later **continues where it stopped**, with no duplicates (tested with
  4,000 mails).
- **Stop all sync / Start all sync** at the top is the master switch for every account. Everything
  is fully stopped within about 30 seconds, and it resumes within 10–20 seconds.
- While stopped, nothing talks to Hostinger for that account. Mail already in Roundcube stays
  readable, and read/unread changes made meanwhile are sent once sync starts again.

**Not responding** means the sync service hasn't reported for 3 minutes. Check that
`mail-sync-pull` and `mail-sync-push` are running with `docker compose ps`.

The page reads everything from the database, so opening it never logs in to Hostinger.
The progress data is in three new tables: `sync_progress`, `sync_account_status` and `sync_settings`.


## Every copy is downloaded, duplicates included

The mirror is an exact copy of Hostinger. If Hostinger holds the same Message-ID twice, whether
it's a true duplicate or two different mails that happen to share an ID, Roundcube gets both.
The sync matches by **count**: it downloads each Hostinger copy that doesn't have a matching
local copy yet. That means:
- a restart or first run never downloads mail that is already in Roundcube;
- a duplicate arriving later is downloaded too;
- sent mail uploaded by sync-push is recognised by its Hostinger number and never downloaded back;
- if Hostinger deletes one of two copies, only one local copy is removed
  (with `DELETE_LOCAL_ON_REMOTE_DELETE=1`).

## Folder counts don't match? (e.g. Sent: Hostinger 1,110, Roundcube 1,061, Waiting 0)

Run the check tool. It compares copy by copy and explains every difference:

```bash
docker exec mail-sync-pull python3 /app/check_folder.py you@domain.com INBOX.Sent
```

| Category | Meaning | Action |
|---|---|---|
| extra copies missing | Hostinger has more copies of a mail (same Message-ID) than Roundcube. The earlier version of the sync collapsed these | `--fetch-missing` |
| never downloaded | a real gap | `--fetch-missing` |
| deleted in Roundcube | downloaded, then deleted or moved in Roundcube. Kept out on purpose | `--fetch-missing --include-deleted` |

```bash
# download everything missing (extra copies + real gaps)
docker exec mail-sync-pull python3 /app/check_folder.py you@domain.com INBOX.Sent --fetch-missing
# ...and also restore mail that was deleted in Roundcube
docker exec mail-sync-pull python3 /app/check_folder.py you@domain.com INBOX.Sent --fetch-missing --include-deleted
```
It lists the first 10 mails in each category (date and subject), downloads only the copies
that are missing, and never changes Hostinger. It works for any folder: leave out the folder name
for INBOX. Put the whole command on **one line**.


## Where do duplicate mails come from?

```bash
docker exec mail-sync-pull python3 /app/find_duplicates.py you@domain.com INBOX.Sent
```
This is read-only. For every mail stored more than once, it shows each copy's UID, its stored
date, "added around" (the date of the mail stored just before that copy, which tells you when the
copy really arrived; the stored date can be copied from the original by upload tools) and which
headers differ. The summary splits duplicates into **saved together** (the sending program or
server saved twice at the same moment) and **added later** (a sync tool, import or re-upload added
a second copy). Add `--details` to list all of them.
