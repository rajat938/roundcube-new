# Security test used during development (runs against a test instance behind Caddy). Not needed in production.
"""Security tests for MY account manager (Caddy HTTPS :8443 -> app :5055)."""
import glob, re, time
import pymysql, requests

BASE = "https://127.0.0.1:8443"
CA = glob.glob("/root/.local/share/caddy/pki/authorities/local/root.crt")[0]
ADMIN, GOOD = "admin", "Correct-Horse-9x"
results = []


def report(name, ok, detail=""):
    results.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def db():
    return pymysql.connect(host="localhost", user="rc", password="rc", database="roundcubemail", autocommit=True)


def S():
    s = requests.Session(); s.verify = CA; s.trust_env = False; return s


def csrf_of(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html); return m.group(1) if m else None


def login(s, user=ADMIN, pw=GOOD, **kw):
    return s.post(f"{BASE}/login", data={"username": user, "password": pw}, allow_redirects=False, **kw)


c = db().cursor()
c.execute("DELETE FROM mirror_accounts WHERE email='rajat@t.com'"); c.execute("DELETE FROM dovecot_users WHERE email='rajat@t.com'")

# 1. HTTPS with a verified certificate; plain HTTP refused
r = S().get(f"{BASE}/login")
report("served over HTTPS (certificate verified against Caddy's CA)", r.status_code == 200)
try:
    rr = requests.get("http://127.0.0.1:8443/login", timeout=5)
    report("plain HTTP on the HTTPS port refused", rr.status_code == 400, f"({rr.status_code})")
except requests.RequestException as e:
    report("plain HTTP on the HTTPS port refused", True, f"({type(e).__name__})")

# 2. signed out -> nothing visible, nothing changeable
s = S()
codes = [s.get(f"{BASE}/", allow_redirects=False).status_code,
         s.post(f"{BASE}/toggle", data={"email": "x@y.com"}, allow_redirects=False).status_code,
         s.post(f"{BASE}/add", data={"email": "x@y.com", "password": "p"}, allow_redirects=False).status_code]
report("signed-out visitor can't see or change anything", codes == [302, 302, 302], f"({codes})")

# 3. headers
h = r.headers
need = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
        "Strict-Transport-Security": "max-age"}
missing = [k for k, v in need.items() if v not in h.get(k, "")]
report("security headers present, server software hidden", not missing and "Server" not in h,
       f"(missing {missing}, Server={h.get('Server')})")

# 4. wrong username vs wrong password: same answer, same timing
t = time.time(); r1 = login(S(), pw="wrong-password"); t1 = time.time() - t
t = time.time(); r2 = login(S(), user="nobody", pw="wrong-password"); t2 = time.time() - t
report("wrong password / wrong username rejected with same timing",
       r1.status_code == 401 and r2.status_code == 401 and abs(t1 - t2) < 0.5, f"({t1:.2f}s vs {t2:.2f}s)")

# 5. good login
s = S()
s.get(f"{BASE}/login")
before = s.cookies.get("__Host-am")
r = login(s)
ck = r.headers.get("Set-Cookie", "")
after = s.cookies.get("__Host-am")
flags_ok = all(f in ck for f in ("__Host-am=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/")) and "Domain" not in ck
report("correct login works; cookie __Host-, Secure, HttpOnly, SameSite=Strict", r.status_code == 302 and flags_ok)
report("new session issued at login (no session fixation)", bool(after) and after != before)
token = csrf_of(s.get(f"{BASE}/").text)

# 6. CSRF / method
a1 = s.post(f"{BASE}/toggle", data={"email": "rajat@t.com"}, allow_redirects=False).status_code
a2 = s.post(f"{BASE}/toggle", data={"email": "rajat@t.com", "csrf": "forged"}, allow_redirects=False).status_code
a3 = s.get(f"{BASE}/toggle", allow_redirects=False).status_code
report("changes without a valid CSRF token rejected; GET can't change anything",
       (a1, a2, a3) == (400, 400, 405), f"({a1}, {a2}, GET {a3})")

# 7. add with WRONG Hostinger password
r = s.post(f"{BASE}/add", data={"email": "rajat@t.com", "password": "not-the-password", "csrf": token})
c.execute("SELECT COUNT(*) FROM mirror_accounts WHERE email='rajat@t.com'"); n = c.fetchone()[0]
report("wrong Hostinger password -> refused, nothing saved", n == 0 and "rejected" in r.text, f"(rows {n})")

# 8. add with RIGHT password
r = s.post(f"{BASE}/add", data={"email": "rajat@t.com", "password": "hpw", "csrf": token})
m = re.search(r"Password <code>([^<]+)</code>", r.text)
shown = m.group(1) if m else None
c.execute("SELECT local_password FROM mirror_accounts WHERE email='rajat@t.com'"); row = c.fetchone()
c.execute("SELECT password FROM dovecot_users WHERE email='rajat@t.com'"); drow = c.fetchone()
report("verified Hostinger login -> account added, Roundcube password shown once",
       bool(shown and row and drow and row[0] == shown == drow[0]))
listing = s.get(f"{BASE}/").text
report("passwords never appear in the account list", shown and shown not in listing and "hpw" not in listing)

# 9. duplicate + injection
r = s.post(f"{BASE}/add", data={"email": "rajat@t.com", "password": "hpw", "csrf": token})
report("duplicate account refused", "already exists" in r.text)
r = s.post(f"{BASE}/add", data={"email": "<script>alert(1)</script>@x.com", "password": "p", "csrf": token})
report("script injection in email refused, not echoed", "<script>alert(1)" not in r.text)

# 10. pause / reset / update
s.post(f"{BASE}/toggle", data={"email": "rajat@t.com", "csrf": token})
c.execute("SELECT active FROM mirror_accounts WHERE email='rajat@t.com'"); act = c.fetchone()[0]
s.post(f"{BASE}/toggle", data={"email": "rajat@t.com", "csrf": token})
report("pause works (POST + CSRF)", act == 0)
r = s.post(f"{BASE}/reset-local", data={"email": "rajat@t.com", "csrf": token})
m2 = re.search(r"Password <code>([^<]+)</code>", r.text)
new_local = m2.group(1) if m2 else None
c.execute("SELECT m.local_password, d.password FROM mirror_accounts m JOIN dovecot_users d USING(email) "
          "WHERE email='rajat@t.com'"); lp, dp = c.fetchone()
report("new Roundcube password: shown once, saved in both tables", new_local and lp == dp == new_local != shown)
r = s.post(f"{BASE}/update-hostinger", data={"email": "rajat@t.com", "password": "wrong", "csrf": token})
c.execute("SELECT hostinger_password FROM mirror_accounts WHERE email='rajat@t.com'"); hp = c.fetchone()[0]
report("wrong new Hostinger password refused, old one kept", hp == "hpw" and "rejected" in r.text)

# 11. tampered cookie, logout
t = S(); t.cookies.set("__Host-am", after[:-4] + "AAAA", domain="127.0.0.1", path="/")
report("tampered session cookie -> treated as signed out",
       t.get(f"{BASE}/", allow_redirects=False).status_code == 302)
s.post(f"{BASE}/logout", data={"csrf": token})
report("sign out ends the session", s.get(f"{BASE}/", allow_redirects=False).status_code == 302)

# 12. brute force
bf = S()
codes = [login(bf, pw=f"guess{i}").status_code for i in range(5)]
locked = login(bf).status_code
spoof = login(bf, headers={"X-Forwarded-For": "8.8.8.8"}).status_code
report("5 wrong passwords -> IP locked, even the correct password refused",
       codes == [401] * 5 and locked == 429, f"({codes} then {locked})")
report("lock can't be dodged with a fake X-Forwarded-For header", spoof == 429, f"({spoof})")

# 13. logs
logtxt = open("/srv/am.log").read()
report("no passwords in the logs", all(p and p not in logtxt for p in ("hpw", GOOD, shown, new_local)))

print(f"\n{sum(results)}/{len(results)} passed")
