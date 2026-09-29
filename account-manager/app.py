"""
Account manager for the Hostinger mirror -- with a proper web login.

Security:
  * Admin login: username + password hash from .env (never a plain password)
  * Guessing protection: 5 wrong passwords from one IP -> that IP is locked
    for 15 minutes; 30 wrong passwords in total -> all logins paused 15 min
  * Sessions: signed cookie, HttpOnly + Secure + SameSite=Strict, 30 min
    idle timeout, 12 h maximum; new session id on every login
  * Every change is a POST with a CSRF token (no more GET /toggle links)
  * Hostinger password is checked against Hostinger (TLS, certificate
    verified) BEFORE it is saved -- typos can't lock the account out
  * Local (Roundcube) passwords are shown ONCE, never listed again
  * No secrets in cookies, logs or URLs; strict security headers; no JS
  * Meant to run behind the HTTPS proxy (caddy service), never on plain HTTP

Commands:
  python3 app.py            run the web app (waitress, port 5000)
  python3 app.py setup      print ADMIN_PASSWORD_HASH and SECRET_KEY for .env
"""
import getpass
import hmac
import imaplib
import logging
import os
import re
import secrets
import ssl
import sys
import threading
import time
from datetime import timedelta

import pymysql
from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("account-manager")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default):
    return str(env(name, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------
#  setup helper (runs without DB / Flask config)
# ----------------------------------------------------------------------
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "setup":
    pw1 = getpass.getpass("New admin password (min 12 characters): ")
    pw2 = getpass.getpass("Repeat: ")
    if pw1 != pw2:
        sys.exit("Passwords don't match.")
    if len(pw1) < 12:
        sys.exit("Use at least 12 characters.")
    print("\nAdd these lines to .env (keep the single quotes):\n")
    print(f"ADMIN_PASSWORD_HASH='{generate_password_hash(pw1)}'")
    print(f"SECRET_KEY='{secrets.token_urlsafe(48)}'")
    sys.exit(0)


# ----------------------------------------------------------------------
#  Settings
# ----------------------------------------------------------------------
DB = dict(host=env("DB_HOST", "roundcubemail-mysql"), user=env("DB_USER"), password=env("DB_PASSWORD"),
          database=env("DB_NAME", "roundcubemail"), charset="utf8mb4")
if not DB["user"] or not DB["password"]:
    sys.exit("DB_USER / DB_PASSWORD are not set -- put them in .env")

ADMIN_USERNAME = env("ADMIN_USERNAME", "admin")
# A real hash never contains "$$"; if it does, docker compose escaping
# doubled the $ signs -- undo that so the login still works.
ADMIN_PASSWORD_HASH = env("ADMIN_PASSWORD_HASH", "").strip().strip("'\"").replace("$$", "$")
if not ADMIN_PASSWORD_HASH or ":" not in ADMIN_PASSWORD_HASH:
    sys.exit("ADMIN_PASSWORD_HASH is not set. Run:\n"
             "  docker compose run --rm account-manager python3 /app/app.py setup\n"
             "and put the printed lines in .env")

SECRET_KEY = env("SECRET_KEY", "")
if len(SECRET_KEY) < 32:
    log.warning("SECRET_KEY missing/short -- using a random one (everyone is logged out on restart). "
                "Run 'app.py setup' and put SECRET_KEY in .env.")
    SECRET_KEY = secrets.token_urlsafe(48)

COOKIE_SECURE = env_bool("COOKIE_SECURE", True)      # only 0 for local testing without HTTPS
TRUST_PROXY = env_bool("TRUST_PROXY", True)           # true when behind the caddy HTTPS proxy
SESSION_IDLE = int(env("SESSION_IDLE_MINUTES", "30")) * 60

LOCK_PER_IP = int(env("LOGIN_MAX_FAILS_PER_IP", "5"))
LOCK_GLOBAL = int(env("LOGIN_MAX_FAILS_TOTAL", "30"))
LOCK_WINDOW = int(env("LOGIN_LOCK_MINUTES", "15")) * 60

HOSTINGER_IMAP_HOST = env("HOSTINGER_IMAP_HOST", "imap.hostinger.com")
HOSTINGER_IMAP_PORT = int(env("HOSTINGER_IMAP_PORT", "993"))
REMOTE_TLS_VERIFY = env_bool("REMOTE_TLS_VERIFY", True)
REMOTE_ALLOWED_HOSTS = {h.strip().lower() for h in env("REMOTE_ALLOWED_HOSTS", "imap.hostinger.com").split(",")
                        if h.strip()}
if HOSTINGER_IMAP_HOST.lower() not in REMOTE_ALLOWED_HOSTS:
    sys.exit(f"HOSTINGER_IMAP_HOST {HOSTINGER_IMAP_HOST} is not in REMOTE_ALLOWED_HOSTS")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,63}$")
# a real hash to compare against when the username is wrong, so a wrong
# username takes exactly as long as a wrong password
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


# ----------------------------------------------------------------------
#  App
# ----------------------------------------------------------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_NAME="__Host-am" if COOKIE_SECURE else "am",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=16 * 1024,
)
if TRUST_PROXY:
    # exactly one proxy (caddy) in front -> trust one X-Forwarded-* hop
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def db_conn():
    return pymysql.connect(**DB, autocommit=False)


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mirror_accounts (
        id INT AUTO_INCREMENT PRIMARY KEY,
        email VARCHAR(255) UNIQUE NOT NULL,
        hostinger_password VARCHAR(255) NOT NULL,
        local_password VARCHAR(255) NOT NULL,
        imap_host VARCHAR(255) DEFAULT 'imap.hostinger.com',
        imap_port INT DEFAULT 993,
        active TINYINT DEFAULT 1,
        last_synced_at DATETIME DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS dovecot_users (
        email VARCHAR(255) PRIMARY KEY,
        password VARCHAR(255) NOT NULL,
        home VARCHAR(255) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sync_progress (
        account_email VARCHAR(255) NOT NULL,
        folder VARCHAR(255) NOT NULL,
        remote_messages INT DEFAULT NULL,
        local_messages INT DEFAULT NULL,
        pending INT NOT NULL DEFAULT 0,
        start_pending INT NOT NULL DEFAULT 0,
        started_at DATETIME DEFAULT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (account_email, folder))""",
    """CREATE TABLE IF NOT EXISTS sync_account_status (
        account_email VARCHAR(255) NOT NULL PRIMARY KEY,
        pull_state VARCHAR(20) DEFAULT NULL,
        pull_detail VARCHAR(255) DEFAULT NULL,
        pull_heartbeat DATETIME DEFAULT NULL,
        push_state VARCHAR(20) DEFAULT NULL,
        push_detail VARCHAR(255) DEFAULT NULL,
        push_heartbeat DATETIME DEFAULT NULL,
        last_new_mail_at DATETIME DEFAULT NULL)""",
    """CREATE TABLE IF NOT EXISTS sync_settings (
        name VARCHAR(64) NOT NULL PRIMARY KEY,
        value VARCHAR(255) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sync_guard (
        account_email VARCHAR(255) NOT NULL,
        guard VARCHAR(64) NOT NULL,
        message VARCHAR(500) NOT NULL,
        item_count INT NOT NULL DEFAULT 0,
        auto_minutes INT DEFAULT NULL,
        first_seen DATETIME NOT NULL,
        approved TINYINT NOT NULL DEFAULT 0,
        PRIMARY KEY (account_email, guard))""",
]


def ensure_schema():
    while True:
        try:
            conn = db_conn()
            with conn.cursor() as cur:
                for stmt in SCHEMA:
                    cur.execute(stmt)
            conn.commit()
            conn.close()
            log.info("[startup] database OK")
            return
        except Exception as e:
            log.warning("[startup] waiting for database (%s)", type(e).__name__)
            time.sleep(5)


# ----------------------------------------------------------------------
#  Login throttling
# ----------------------------------------------------------------------
_fails_lock = threading.Lock()
_fails_by_ip = {}
_fails_all = []


def _prune(now):
    global _fails_all
    _fails_all = [t for t in _fails_all if now - t < LOCK_WINDOW]
    for ip in list(_fails_by_ip):
        _fails_by_ip[ip] = [t for t in _fails_by_ip[ip] if now - t < LOCK_WINDOW]
        if not _fails_by_ip[ip]:
            del _fails_by_ip[ip]


def login_blocked(ip):
    now = time.time()
    with _fails_lock:
        _prune(now)
        return len(_fails_by_ip.get(ip, [])) >= LOCK_PER_IP or len(_fails_all) >= LOCK_GLOBAL


def record_fail(ip):
    now = time.time()
    with _fails_lock:
        _fails_by_ip.setdefault(ip, []).append(now)
        _fails_all.append(now)


def clear_fails(ip):
    with _fails_lock:
        _fails_by_ip.pop(ip, None)


# ----------------------------------------------------------------------
#  Auth / CSRF
# ----------------------------------------------------------------------
def client_ip():
    return request.remote_addr or "?"


def logged_in():
    user = session.get("user")
    if not user:
        return False
    now = time.time()
    if now - session.get("seen", 0) > SESSION_IDLE:
        session.clear()
        return False
    session["seen"] = now
    return True


def check_csrf():
    sent = request.form.get("csrf", "")
    real = session.get("csrf", "")
    if not real or not hmac.compare_digest(sent, real):
        log.warning("CSRF check failed from %s on %s", client_ip(), request.path)
        abort(400)


@app.before_request
def guard():
    if request.endpoint in ("login", "static", "health"):
        return None
    if not logged_in():
        return redirect(url_for("login"))
    if request.method == "POST":
        check_csrf()
    return None


@app.after_request
def headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'")
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    if COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


# ----------------------------------------------------------------------
#  Hostinger credential check (TLS, certificate verified)
# ----------------------------------------------------------------------
def verify_hostinger(email_addr, password):
    """(True, '') if Hostinger accepts the login, else (False, reason)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not REMOTE_TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        conn = imaplib.IMAP4_SSL(HOSTINGER_IMAP_HOST, HOSTINGER_IMAP_PORT, ssl_context=ctx, timeout=20)
    except Exception as e:
        return False, f"Could not reach Hostinger ({type(e).__name__}). Nothing was saved."
    try:
        conn.login(email_addr, password)
        return True, ""
    except imaplib.IMAP4.error:
        return False, "Hostinger rejected this email/password. Nothing was saved."
    except Exception as e:
        return False, f"Hostinger check failed ({type(e).__name__}). Nothing was saved."
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def new_local_password():
    return secrets.token_urlsafe(12)


# ----------------------------------------------------------------------
#  Pages
# ----------------------------------------------------------------------
BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mail Account Manager</title>
<style>
 :root{--bg:#f6f7f9;--card:#fff;--ink:#1c2330;--mut:#5b6474;--line:#dde1e7;--acc:#1f6feb;--ok:#1a7f37;--bad:#c62828}
 @media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c23;--ink:#e6e9ee;--mut:#9aa4b2;--line:#2a313b;--acc:#4c8dff;--ok:#3fb950;--bad:#ff6b6b}}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
 .wrap{max-width:980px;margin:0 auto;padding:24px 16px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}
 h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 12px}
 .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px}
 input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);width:100%}
 label{display:block;font-size:13px;color:var(--mut);margin:8px 0 4px}
 button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
 button.sec{background:transparent;color:var(--ink);border-color:var(--line)}
 button.small{padding:4px 10px;font-size:13px}
 .grid{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end}
 @media (max-width:640px){.grid{grid-template-columns:1fr}}
 table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}
 th{font-size:12px;color:var(--mut);font-weight:600;border-top:0}
 .tablewrap{overflow-x:auto}
 .msg{padding:10px 12px;border-radius:8px;margin-bottom:12px;border:1px solid}
 .msg.ok{border-color:var(--ok)}.msg.error{border-color:var(--bad)}
 .pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
 .on{color:var(--ok);border-color:var(--ok)}.off{color:var(--mut)}
 code{background:var(--bg);padding:2px 6px;border-radius:4px;font-size:14px;user-select:all}
 .actions form{display:inline-block;margin:0 4px 4px 0}
 .inline{display:flex;gap:6px}.inline input{width:170px}
 .login{max-width:380px;margin:10vh auto}
 .mut{color:var(--mut);font-size:13px}
 .row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
 .acct h2{margin:0;font-size:16px;word-break:break-all}
 .bar{height:10px;background:var(--line);border-radius:99px;overflow:hidden;margin:10px 0 6px}
 .bar span{display:block;height:100%;background:var(--acc)}
 .bar.done span{background:var(--ok)}
 .nums{display:flex;gap:18px;flex-wrap:wrap;font-size:14px}.nums b{font-variant-numeric:tabular-nums}
 .warn{color:#b26a00;border-color:#b26a00}.bad{color:var(--bad);border-color:var(--bad)}
 details{margin-top:10px}summary{cursor:pointer;color:var(--mut);font-size:13px}
 details table{margin-top:6px;font-size:13px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
 .acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;align-items:center}
 .acts form{margin:0}
 .banner{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
 a{color:var(--acc)}
 .hold{border:1px solid #b26a00;border-radius:8px;padding:10px 12px;margin-top:10px}
 .hold .row{align-items:flex-start}
</style>{% if refresh %}<meta http-equiv="refresh" content="10">{% endif %}</head><body><div class="wrap">
{% with msgs = get_flashed_messages(with_categories=true) %}{% for cat, m in msgs %}
<div class="msg {{cat}}">{{m}}</div>{% endfor %}{% endwith %}
{{ body|safe }}
</div></body></html>"""

LOGIN = """<div class="card login"><h1>Mail Account Manager</h1><p class="mut">Sign in to continue.</p>
<form method="post" autocomplete="off">
<label for="u">Username</label><input id="u" name="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" required>
<p><button type="submit">Sign in</button></p></form></div>"""

MAIN = """<div class="top"><h1>Mirrored Hostinger accounts</h1>
<form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf" value="{{csrf}}">
<button class="sec small" type="submit">Sign out ({{user}})</button></form></div>

{% if shown %}<div class="msg ok"><b>{{shown.title}}</b> Roundcube login for <b>{{shown.email}}</b>:<br>
Username <code>{{shown.email}}</code> &nbsp; Password <code>{{shown.password}}</code><br>
<span class="mut">Copy it now -- it is not shown again.</span></div>{% endif %}

<div class="card banner"><div>
{% if paused %}<span class="pill bad">Sync stopped</span> <span class="mut">No account is syncing. Mail already in Roundcube stays readable.</span>
{% else %}<span class="pill on">Sync running</span> <span class="mut">{{running}} of {{accounts|length}} account(s) syncing</span>{% endif %}
</div><div class="acts" style="margin:0">
{% if refresh %}<a class="mut" href="{{ url_for('index') }}">Stop auto-refresh</a>
{% else %}<a class="mut" href="{{ url_for('index', refresh=1) }}">Auto-refresh every 10s</a>{% endif %}
<form method="post" action="{{ url_for('sync_all') }}"><input type="hidden" name="csrf" value="{{csrf}}">
<input type="hidden" name="action" value="{{'start' if paused else 'stop'}}">
<button class="{{'' if paused else 'sec'}} small">{{'Start all sync' if paused else 'Stop all sync'}}</button></form>
</div></div>

{% for a in accounts %}<div class="card acct">
<div class="row"><h2>{{a.email}}</h2><span class="pill {{a.cls}}">{{a.label}}</span></div>
<div class="mut">{{a.detail}}</div>
{% for g in a.holds %}<div class="hold"><div class="row"><div><b>Waiting for you:</b> {{g.message}}<br>
<span class="mut">Since {{g.since}}{% if g.auto %} -- goes ahead by itself {{g.auto}} if nothing changes{% endif %}{% if g.approved %} -- approved, applying at the next check{% endif %}</span></div>
{% if not g.approved %}<form method="post" action="{{ url_for('approve') }}"><input type="hidden" name="csrf" value="{{csrf}}">
<input type="hidden" name="email" value="{{a.email}}"><input type="hidden" name="guard" value="{{g.guard}}">
<button class="small">Approve</button></form>{% endif %}</div></div>{% endfor %}
{% if a.remote %}
<div class="bar {{'done' if a.pending == 0 else ''}}"><span style="width:{{a.pct}}%"></span></div>
<div class="nums"><span><b>{{a.pct_text}}</b> synced</span>
<span>On Hostinger <b>{{"{:,}".format(a.remote)}}</b></span>
<span>In Roundcube <b>{{"{:,}".format(a.local)}}</b></span>
<span>Waiting to download <b>{{"{:,}".format(a.pending)}}</b></span>
{% if a.eta %}<span>About <b>{{a.eta}}</b> left</span>{% endif %}</div>
<details><summary>Folders ({{a.folders|length}})</summary><div class="tablewrap"><table>
<tr><th>Folder</th><th class="n">On Hostinger</th><th class="n">In Roundcube</th><th class="n">Waiting</th><th>Updated</th></tr>
{% for f in a.folders %}<tr><td>{{f.name}}</td><td class="n">{{f.remote_text}}</td><td class="n">{{f.local_text}}</td>
<td class="n">{{"{:,}".format(f.pending)}}</td><td class="mut">{{f.age}}</td></tr>{% endfor %}
</table></div><p class="mut">"In Roundcube" can differ from Hostinger on purpose: mail you delete in Roundcube stays on Hostinger.</p></details>
{% else %}<p class="mut">No progress reported yet{{' -- sync is stopped' if not a.syncing else ' -- the sync service picks this account up within a few seconds'}}.</p>{% endif %}
<div class="mut">Sending/read-status sync: {{a.push}}</div>
<div class="acts">
<form method="post" action="{{ url_for('toggle') }}"><input type="hidden" name="csrf" value="{{csrf}}">
<input type="hidden" name="email" value="{{a.email}}"><button class="{{'sec' if a.active else ''}} small">{{'Stop sync' if a.active else 'Start sync'}}</button></form>
<form method="post" action="{{ url_for('reset_local') }}"><input type="hidden" name="csrf" value="{{csrf}}">
<input type="hidden" name="email" value="{{a.email}}"><button class="sec small">New Roundcube password</button></form>
<form method="post" action="{{ url_for('update_hostinger') }}" class="inline" autocomplete="off">
<input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="email" value="{{a.email}}">
<input name="password" type="password" placeholder="New Hostinger password" required maxlength="256">
<button class="sec small">Update</button></form>
<span class="mut">Added {{a.created}}</span></div>
</div>{% else %}<div class="card mut">No accounts yet.</div>{% endfor %}

{% if not refresh %}<div class="card"><h2>Add account</h2>
<form method="post" action="{{ url_for('add') }}" autocomplete="off"><input type="hidden" name="csrf" value="{{csrf}}">
<div class="grid"><div><label for="e">Email</label><input id="e" name="email" type="email" required maxlength="254"></div>
<div><label for="hp">Hostinger password</label><input id="hp" name="password" type="password" required maxlength="256"></div>
<div><button type="submit">Check with Hostinger &amp; add</button></div></div></form>
<p class="mut">The password is verified with Hostinger over an encrypted connection before anything is saved.</p></div>{% endif %}"""


def page(body_tpl, **ctx):
    body = render_template_string(body_tpl, **ctx)
    return render_template_string(BASE, body=body, refresh=ctx.get("refresh", False))


STALE_SECONDS = 180  # no heartbeat for this long -> "not responding"


def _ago(seconds):
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "1 min"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600} h {round((seconds % 3600) / 60)} min"


def sync_paused(cur):
    cur.execute("SELECT value FROM sync_settings WHERE name='paused'")
    row = cur.fetchone()
    return bool(row and row[0] == "1")


def list_accounts():
    """Accounts + live sync progress, read only from the database (opening
    this page never logs in to Hostinger)."""
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            paused = sync_paused(cur)
            cur.execute("SELECT email, active, created_at FROM mirror_accounts ORDER BY email")
            accounts = [dict(email=e, active=bool(a), created=c) for e, a, c in cur.fetchall()]
            cur.execute("SELECT account_email, pull_state, pull_detail, TIMESTAMPDIFF(SECOND, pull_heartbeat, NOW()), "
                        "push_state, push_detail, TIMESTAMPDIFF(SECOND, push_heartbeat, NOW()) "
                        "FROM sync_account_status")
            status = {r[0]: r[1:] for r in cur.fetchall()}
            cur.execute("SELECT account_email, folder, remote_messages, local_messages, pending, start_pending, "
                        "TIMESTAMPDIFF(SECOND, started_at, NOW()), TIMESTAMPDIFF(SECOND, updated_at, NOW()) "
                        "FROM sync_progress ORDER BY account_email, folder!='INBOX', folder")
            progress = {}
            for row in cur.fetchall():
                progress.setdefault(row[0], []).append(row[1:])
            cur.execute("SELECT account_email, guard, message, auto_minutes, approved, "
                        "TIMESTAMPDIFF(SECOND, first_seen, NOW()) FROM sync_guard ORDER BY first_seen")
            holds = {}
            for em, guard, message, auto, approved, age in cur.fetchall():
                left = (auto * 60 - (age or 0)) if auto is not None else None
                holds.setdefault(em, []).append(dict(
                    guard=guard, message=message, approved=bool(approved), since=_ago(age),
                    auto=(f"in about {_duration(max(left, 60))}" if left is not None and left > 0
                          else ("at the next check" if left is not None else ""))))
    finally:
        conn.close()

    for a in accounts:
        a["holds"] = holds.get(a["email"], [])
        st = status.get(a["email"])
        a["syncing"] = a["active"] and not paused
        # --- headline status
        if paused:
            a["label"], a["cls"], a["detail"] = "Stopped", "bad", "All sync is stopped (switch at the top)."
        elif not a["active"]:
            a["label"], a["cls"], a["detail"] = "Stopped", "bad", "Sync stopped for this account."
        elif not st or st[2] is None:
            a["label"], a["cls"], a["detail"] = "Starting", "warn", "Waiting for the sync service to pick it up."
        elif st[2] > STALE_SECONDS:
            a["label"], a["cls"] = "Not responding", "bad"
            a["detail"] = (f"No update from the sync service for {_ago(st[2]).replace(' ago', '')}. "
                           f"Is the mail-sync-pull container running?")
        else:
            state, detail = st[0] or "", st[1] or ""
            a["label"] = {"up to date": "Up to date", "syncing": "Syncing", "connecting": "Connecting",
                          "error": "Problem", "stopped": "Stopped"}.get(state, state.title())
            a["cls"] = {"up to date": "on", "error": "bad", "stopped": "bad"}.get(state, "warn")
            a["detail"] = detail
        if a["holds"] and a["syncing"]:
            a["label"], a["cls"] = "Waiting for you", "warn"
            a["detail"] = "A bulk change is on hold -- see below."
        # --- push line
        if not a["syncing"]:
            a["push"] = "stopped"
        elif not st or st[5] is None:
            a["push"] = "waiting for the sync service"
        elif st[5] > STALE_SECONDS:
            a["push"] = "not responding (is the mail-sync-push container running?)"
        else:
            a["push"] = f"{(st[3] or '').replace('up to date', 'working')} -- {st[4] or ''}"
        # --- numbers
        folders, remote, local, pending, eta = [], 0, 0, 0, 0
        for name, r, l, p, sp, elapsed, age in progress.get(a["email"], []):
            remote += r or 0
            local += l or 0
            pending += p or 0
            if p and sp and elapsed and sp > p:
                rate = (sp - p) / max(elapsed, 1)
                eta += p / rate if rate > 0 else 0
            folders.append(dict(name=name, pending=p or 0, age=_ago(age),
                                remote_text="-" if r is None else f"{r:,}",
                                local_text="-" if l is None else f"{l:,}"))
        a.update(folders=folders, remote=remote, local=local, pending=pending)
        done = max(remote - pending, 0)
        pct = 100.0 if not remote else min(100.0, 100.0 * done / remote)
        a["pct"] = f"{pct:.1f}"
        a["pct_text"] = f"{pct:.0f}%" if pct >= 1 or pct == 0 else f"{pct:.1f}%"
        a["eta"] = _duration(eta) if pending and eta else ""
    return accounts, paused


def main_page(shown=None, refresh=False):
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    accounts, paused = list_accounts()
    running = sum(1 for a in accounts if a["syncing"])
    return page(MAIN, accounts=accounts, paused=paused, running=running, csrf=session["csrf"],
                user=session.get("user"), shown=shown, refresh=refresh)


@app.get("/health")
def health():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return page(LOGIN)
    ip = client_ip()
    if login_blocked(ip):
        log.warning("login BLOCKED (too many failures) from %s", ip)
        flash("Too many failed sign-ins. Try again in a few minutes.", "error")
        return page(LOGIN), 429
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    user_ok = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
    pw_ok = check_password_hash(ADMIN_PASSWORD_HASH if user_ok else _DUMMY_HASH, password)
    if user_ok and pw_ok:
        clear_fails(ip)
        session.clear()                       # fresh session on every login
        session.permanent = True
        session["user"] = ADMIN_USERNAME
        session["csrf"] = secrets.token_urlsafe(32)
        session["seen"] = time.time()
        log.info("login OK user=%s from %s", ADMIN_USERNAME, ip)
        return redirect(url_for("index"))
    record_fail(ip)
    log.warning("login FAILED from %s", ip)
    time.sleep(1)
    flash("Wrong username or password.", "error")
    return page(LOGIN), 401


@app.post("/logout")
def logout():
    log.info("logout user=%s from %s", session.get("user"), client_ip())
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    return main_page(refresh=request.args.get("refresh") == "1")


@app.post("/approve")
def approve():
    email_addr = form_email()
    guard = request.form.get("guard", "")
    if not email_addr or not re.fullmatch(r"[a-z]+(-[a-z]+)?(:[A-Za-z0-9._ -]{1,200})?", guard):
        abort(400)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            n = cur.execute("UPDATE sync_guard SET approved=1 WHERE account_email=%s AND guard=%s",
                            (email_addr, guard))
        conn.commit()
    finally:
        conn.close()
    if n:
        log.info("HELD ACTION APPROVED %s / %s by %s", email_addr, guard, session.get("user"))
        flash("Approved -- the sync applies it at its next check (usually within 30 seconds).", "ok")
    return redirect(url_for("index"))


@app.post("/sync")
def sync_all():
    action = request.form.get("action")
    if action not in ("start", "stop"):
        abort(400)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sync_settings (name, value) VALUES ('paused', %s) "
                        "ON DUPLICATE KEY UPDATE value=VALUES(value)", ("1" if action == "stop" else "0",))
        conn.commit()
    finally:
        conn.close()
    log.info("ALL SYNC %s by %s", "STOPPED" if action == "stop" else "STARTED", session.get("user"))
    flash("Stopping all sync -- running downloads stop within seconds, everything is fully stopped "
          "within about 30 seconds." if action == "stop"
          else "Sync started. Accounts reconnect within about 10-20 seconds.", "ok")
    return redirect(url_for("index"))


def form_email():
    email_addr = request.form.get("email", "").strip().lower()
    if len(email_addr) > 254 or not EMAIL_RE.match(email_addr):
        return None
    return email_addr


@app.post("/add")
def add():
    email_addr = form_email()
    password = request.form.get("password", "")
    if not email_addr or not (1 <= len(password) <= 256):
        flash("Enter a valid email address and password.", "error")
        return redirect(url_for("index"))
    ok, why = verify_hostinger(email_addr, password)
    if not ok:
        log.info("add %s by %s: Hostinger check failed", email_addr, session.get("user"))
        flash(why, "error")
        return redirect(url_for("index"))
    local_pw = new_local_password()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO mirror_accounts (email, hostinger_password, local_password, imap_host, imap_port) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (email_addr, password, local_pw, HOSTINGER_IMAP_HOST, HOSTINGER_IMAP_PORT))
            cur.execute("INSERT INTO dovecot_users (email, password, home) VALUES (%s,%s,%s)",
                        (email_addr, local_pw, f"/var/mail/{email_addr}"))
        conn.commit()
    except pymysql.err.IntegrityError:
        conn.rollback()
        flash(f"{email_addr} already exists.", "error")
        return redirect(url_for("index"))
    finally:
        conn.close()
    log.info("account ADDED %s by %s", email_addr, session.get("user"))
    return main_page(shown=dict(title="Account added.", email=email_addr, password=local_pw))


@app.post("/toggle")
def toggle():
    email_addr = form_email()
    if not email_addr:
        abort(400)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            n = cur.execute("UPDATE mirror_accounts SET active = 1 - active WHERE email=%s", (email_addr,))
        conn.commit()
    finally:
        conn.close()
    if n:
        log.info("account TOGGLED %s by %s", email_addr, session.get("user"))
        flash(f"Sync for {email_addr} changed. It takes effect within about 10-30 seconds.", "ok")
    return redirect(url_for("index"))


@app.post("/reset-local")
def reset_local():
    email_addr = form_email()
    if not email_addr:
        abort(400)
    local_pw = new_local_password()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            n = cur.execute("UPDATE mirror_accounts SET local_password=%s WHERE email=%s", (local_pw, email_addr))
            cur.execute("UPDATE dovecot_users SET password=%s WHERE email=%s", (local_pw, email_addr))
        conn.commit()
    finally:
        conn.close()
    if not n:
        flash("No such account.", "error")
        return redirect(url_for("index"))
    log.info("Roundcube password RESET for %s by %s", email_addr, session.get("user"))
    return main_page(shown=dict(title="New Roundcube password.", email=email_addr, password=local_pw))


@app.post("/update-hostinger")
def update_hostinger():
    email_addr = form_email()
    password = request.form.get("password", "")
    if not email_addr or not (1 <= len(password) <= 256):
        abort(400)
    ok, why = verify_hostinger(email_addr, password)
    if not ok:
        flash(why, "error")
        return redirect(url_for("index"))
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            n = cur.execute("UPDATE mirror_accounts SET hostinger_password=%s WHERE email=%s", (password, email_addr))
        conn.commit()
    finally:
        conn.close()
    if n:
        log.info("Hostinger password UPDATED for %s by %s", email_addr, session.get("user"))
        flash(f"Hostinger password for {email_addr} updated. The sync reconnects within a minute.", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    from waitress import serve
    ensure_schema()
    port = int(env("PORT", "5000"))
    log.info("account manager on :%s (admin user: %s, secure cookies: %s)", port, ADMIN_USERNAME, COOKIE_SECURE)
    serve(app, host="0.0.0.0", port=port, threads=8, ident=None)
