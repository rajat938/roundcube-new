#!/bin/sh
set -e
mkdir -p /var/mail /var/mail-indexes
chown -R 5000:5000 /var/mail /var/mail-indexes

# Write the SQL config from the template, taking the DB credentials from the
# environment (.env) instead of a file committed to git.
if [ -z "$DB_PASSWORD" ]; then
    echo "DB_PASSWORD is not set -- add it to .env and pass it to the dovecot service" >&2
    exit 1
fi
esc() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }
sed -e "s|__DB_PASSWORD__|$(esc "$DB_PASSWORD")|" \
    -e "s|__DB_USER__|$(esc "${DB_USER:-roundcube}")|" \
    -e "s|__DB_NAME__|$(esc "${DB_NAME:-roundcubemail}")|" \
    /etc/dovecot/dovecot-sql.conf.ext.template > /etc/dovecot/dovecot-sql.conf.ext
chown root:dovecot /etc/dovecot/dovecot-sql.conf.ext 2>/dev/null || true
chmod 640 /etc/dovecot/dovecot-sql.conf.ext

exec /usr/sbin/dovecot -F
