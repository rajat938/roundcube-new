#!/usr/bin/env bash
# One-time repo upgrade. Run from the project folder AFTER copying the new
# files over it:
#     bash tools/upgrade_repo.sh
#
# It is safe to run more than once. It:
#   1. backs up .env (.env.backup-<date>, never committed)
#   2. moves Roundcube's des_key from config.inc.php into .env
#   3. checks .env has everything docker-compose.yml needs
#   4. removes duplicate lines from .env (keeps the LAST value of each key)
#   5. removes the secrets + unsafe settings from www/config/config.inc.php
#   6. stops git tracking the secret files (they STAY on disk, just leave git)
#   7. deletes old, unused files
#   8. checks that docker compose accepts the result
# It does NOT commit, push, or restart anything -- you review, then do that.
set -euo pipefail

say()  { printf '\n== %s\n' "$*"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")/.."
[ -f docker-compose.yml ] && [ -d .git ] || die "run this inside the project folder (docker-compose.yml + .git)"
[ -f .env ] || die "no .env here -- copy .env.example to .env and fill it in first"

if command -v python3 >/dev/null 2>&1; then
    PY=(python3)
else
    PY=(docker run --rm -v "$PWD":/w -w /w python:3.12-slim python3)
fi

# last value of KEY in .env (empty if missing)
getv() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }

say "1. Backing up .env"
backup=".env.backup-$(date +%Y%m%d-%H%M%S)"
cp -p .env "$backup"
chmod 600 "$backup" .env
echo "   saved $backup (git ignores it)"

say "2. Roundcube des_key -> .env"
if [ -n "$(getv ROUNDCUBE_DES_KEY)" ]; then
    echo "   ROUNDCUBE_DES_KEY already in .env"
else
    key="$("${PY[@]}" - <<'PYEOF'
import re
try:
    s = open("www/config/config.inc.php", encoding="utf-8").read()
except OSError:
    s = ""
m = re.search(r"^\s*\$config\['des_key'\]\s*=\s*'([^']*)'\s*;", s, re.M)
print(m.group(1) if m else "")
PYEOF
)"
    [ -n "$key" ] || die "no des_key in www/config/config.inc.php and none in .env -- add ROUNDCUBE_DES_KEY=... (24 chars) to .env"
    printf "\n# moved from www/config/config.inc.php by upgrade_repo.sh\nROUNDCUBE_DES_KEY='%s'\n" "$key" >> .env
    echo "   copied des_key into .env (same value -- nobody gets logged out)"
fi

say "3. Checking .env has what docker-compose.yml needs"
missing=()
for k in DB_PASSWORD MYSQL_ROOT_PASSWORD ROUNDCUBE_DES_KEY ADMIN_PASSWORD_HASH SECRET_KEY; do
    v="$(getv "$k")"; v="${v#\'}"; v="${v%\'}"
    if [ -z "$v" ] || [ "$v" = "paste-from-setup" ] || [ "$v" = "change-me" ] || [ "$v" = "change-me-too" ]; then
        missing+=("$k")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    die "set these in .env, then run this script again: ${missing[*]}
   MYSQL_ROOT_PASSWORD = your MariaDB root password (the value that used to be in docker-compose.yml)
   ADMIN_PASSWORD_HASH / SECRET_KEY: docker compose run --rm account-manager python3 /app/app.py setup
   (so far only the .env backup was made and des_key was added to .env)"
fi
echo "   all present"

say "4. Removing duplicate lines from .env (keeping the last value of each key)"
"${PY[@]}" - <<'PYEOF'
lines = open(".env", encoding="utf-8").read().splitlines()
last = {}
for i, line in enumerate(lines):
    s = line.strip()
    if s and not s.startswith("#") and "=" in s:
        last[s.split("=", 1)[0].strip()] = i
out, dropped = [], []
for i, line in enumerate(lines):
    s = line.strip()
    if s and not s.startswith("#") and "=" in s and last[s.split("=", 1)[0].strip()] != i:
        dropped.append(s.split("=", 1)[0].strip())
        continue
    out.append(line)
open(".env", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("   removed earlier copies of: " + (", ".join(dropped) if dropped else "(none)"))
PYEOF

say "5. Removing secrets + disabled certificate checks from www/config/config.inc.php"
if [ -f www/config/config.inc.php ]; then
    "${PY[@]}" tools/scrub_roundcube_config.py www/config/config.inc.php | sed 's/^/   /' \
        | sed -E "s/(ROUNDCUBE_DES_KEY=).*/\1(already in .env)/"
fi

say "6. Stopping git tracking of secret files (they stay on disk)"
for f in .env www/config/config.docker.inc.php www/config/config.old.php \
         dovecot/conf/dovecot-sql.conf.ext; do
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
        git rm -q --cached -- "$f" && echo "   untracked $f"
    fi
done

say "7. Deleting old, unused files"
for f in monitor nginx sync-daemon docker q "#Uf03aq" "roundcube/Dockerfile - Copy" \
         dovecot/conf/users dovecot/conf/dovecot-sql.conf.ext \
         setup.md doc-setupmail.txt identity_switch.sql schema.sql core_mysql.initial.sql \
         plugins/sqlite.initial.sql www/plugins/save_sent_to_hostinger \
         "www/skins/elastic-orange (4).zip" www/config/config.old.php; do
    if [ -e "$f" ]; then
        git rm -r -q --cached --ignore-unmatch -- "$f" >/dev/null 2>&1 || true
        rm -rf -- "$f"
        echo "   deleted $f"
    fi
done
for z in www/vendor/composer/tmp-*.zip; do
    [ -e "$z" ] || continue
    git rm -q --cached --ignore-unmatch -- "$z" >/dev/null 2>&1 || true
    rm -f -- "$z"; echo "   deleted $z"
done
[ -d plugins ] && [ -z "$(ls -A plugins)" ] && rmdir plugins && echo "   deleted empty plugins/"

say "8. Checking docker compose accepts the configuration"
if docker compose config --quiet; then
    echo "   docker-compose.yml + .env: OK"
else
    die "docker compose rejected the configuration (see above)"
fi

say "Done. Next:"
cat <<'EOF'
   git add -A && git status        # review: .env and config files must show as 'deleted' (untracked), not modified
   git commit -m "Security cleanup: secrets out of git, current config"
   git push
   docker compose up -d --build --remove-orphans   # or let the Deploy workflow do it

   STILL TO DO BY HAND (the old secrets are in git history and the repo was public):
   - make the GitHub repo private
   - change the DB + MariaDB root passwords, then update .env
   - run 'app.py setup' again for a new admin password + SECRET_KEY
   - remove the old secrets from git history (git filter-repo) or start a fresh repo
EOF
