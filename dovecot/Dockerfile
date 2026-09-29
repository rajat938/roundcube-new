FROM dovecot/dovecot:2.3-latest

COPY conf/ /etc/dovecot/
COPY entrypoint.sh /entrypoint.sh
# strip Windows line endings (CRLF) so the script always runs
RUN sed -i 's/\r$//' /entrypoint.sh /etc/dovecot/*.conf /etc/dovecot/*.template && chmod +x /entrypoint.sh && rm -f /etc/dovecot/dovecot-sql.conf.ext

# Runs as root (image default) so it can chown /var/mail and write the SQL
# config before starting Dovecot; Dovecot itself drops to uid 5000.
ENTRYPOINT ["/entrypoint.sh"]
