# Pinned: "latest" could silently jump to a new major version on rebuild.
FROM roundcube/roundcubemail:1.7.4-apache

RUN apt-get update -qq \
    && apt-get install -y -qq ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*


RUN if ! php -m | grep -qi '^redis$'; then \
        apt-get update -qq \
        && apt-get install -y -qq --no-install-recommends $PHPIZE_DEPS \
        && pecl install redis \
        && docker-php-ext-enable redis \
        && apt-get purge -y -qq $PHPIZE_DEPS \
        && apt-get autoremove -y -qq \
        && rm -rf /var/lib/apt/lists/* ; \
    fi


COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN  chmod +x /docker-entrypoint.sh 
