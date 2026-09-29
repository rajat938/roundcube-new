<?php

/* Local configuration for Roundcube Webmail */

// ----------------------------------
// IMAP
// ----------------------------------
// The IMAP host (and optionally port number) chosen to perform the log-in.
// Leave blank to show a textbox at login, give a list of hosts
// to display a pulldown menu or set one host as string.
// Enter hostname with prefix ssl:// to use Implicit TLS, or tls:// for STARTTLS.
// If port number is omitted it will be set to 993 (for ssl://) or 143 otherwise.
// Supported replacement variables:
// %n - hostname ($_SERVER['SERVER_NAME'])
// %t - hostname without the first part
// %d - domain (http hostname, $_SERVER['HTTP_HOST'] without the first part)
// %s - domain name after the '@' from e-mail address provided at login screen
// For example %n = mail.domain.tld, %t = domain.tld
// WARNING: After hostname change update of mail_host column in users table is
//          required to match old user data records with the new host.
$config['imap_host'] = 'dovecot';

// ----------------------------------
// SQL DATABASE
// ----------------------------------
// Database connection string (DSN) for read+write operations
// Format (compatible with PEAR MDB2): db_provider://user:password@host/database
// Currently supported db_providers: mysql, pgsql, sqlite
// For examples see https://pear.php.net/manual/en/package.database.mdb2.intro-dsn.php
// Note: for SQLite use absolute path (Linux): 'sqlite:////full/path/to/sqlite.db?mode=0646'
//       or (Windows): 'sqlite:///C:/full/path/to/sqlite.db'
// Note: Various drivers support various additional arguments for connection,
//       for Mysql: key, cipher, cert, capath, ca, verify_server_cert, emulate_prepares
//       for Postgres: application_name, sslmode, sslcert, sslkey, sslrootcert, sslcrl, sslcompression, service.
//       e.g. 'mysql://roundcube:@localhost/roundcubemail?verify_server_cert=false'
// db_dsnw: removed -- now comes from .env / defaults

// ----------------------------------
// LOGGING/DEBUGGING
// ----------------------------------
// log driver:  'syslog', 'stdout', 'file', or 'php'.
$config['log_driver'] = 'stdout';

// IMAP socket context options
// See https://php.net/manual/en/context.ssl.php
// The example below enables server certificate validation
//
// proxy_protocol is used to inject HAproxy style headers in the TCP stream
// See https://www.haproxy.org/download/1.6/doc/proxy-protocol.txt
// WARNING: Please note this is currently incompatible with implicit ssl,
// since the proxy protocol preamble is expected before the ssl handshake.
// $config['imap_conn_options'] = [
//    'ssl' => [
//        'verify_peer'  => true,
//        'verify_depth' => 3,
//        'cafile'       => '/etc/openssl/certs/ca.crt',
//    ],
//    'proxy_protocol' => 1 | 2 | [ // required (either version number (1|2) or array with 'version' key)
//        'version'       => 1 | 2, // required, if array
//        'remote_addr'   => $_SERVER['REMOTE_ADDR'], // optional
//        'remote_port'   => $_SERVER['REMOTE_PORT'], // optional
//        'local_addr'    => $_SERVER['SERVER_ADDR'], // optional
//        'local_port'    => $_SERVER['SERVER_PORT'], // optional
//    ],
// ];
// Note: These can be also specified as an array of options indexed by hostname
// imap_conn_options: removed -- now comes from .env / defaults

// Type of IMAP indexes cache. Supported values: 'db', 'apcu', 'redis' and 'memcache' or 'memcached'.
$config['imap_cache'] = null;

// Enables messages cache. Only 'db' cache is supported.
// This requires an IMAP server that supports QRESYNC and CONDSTORE
// extensions (RFC7162). See synchronize() in program/lib/Roundcube/rcube_imap_cache.php
// for further info, or if you experience syncing problems.
$config['messages_cache'] = false;



$config['ident_switch_check_unseen'] = false;

// ----------------------------------
// SMTP
// ----------------------------------
// SMTP server host (and optional port number) for sending mails.
// Enter hostname with prefix ssl:// to use Implicit TLS, or tls:// for STARTTLS.
// If port number is omitted it will be set to 465 (for ssl://) or 587 otherwise.
// Supported replacement variables:
// %h - user's IMAP hostname
// %n - hostname ($_SERVER['SERVER_NAME'])
// %t - hostname without the first part
// %d - domain (http hostname, $_SERVER['HTTP_HOST'] without the first part)
// %z - IMAP domain (IMAP hostname without the first part)
// For example %n = mail.domain.tld, %t = domain.tld
// To specify different SMTP servers for different IMAP hosts provide an array
// of IMAP host (no prefix or port) and SMTP server e.g. ['imap.example.com' => 'smtp.example.net']
$config['smtp_host'] = 'ssl://smtp.hostinger.com:465';

// Force SSL context bypass globally for stream sockets (ident_switch fix)
// smtp_conn_options: removed -- now comes from .env / defaults

// provide an URL where a user can get support for this Roundcube installation
// PLEASE DO NOT LINK TO THE ROUNDCUBE.NET WEBSITE HERE!
$config['support_url'] = '';

// Location of temporary saved files such as attachments and cache files
// must be writeable for the user who runs PHP process (Apache user if mod_php is being used)
$config['temp_dir'] = '/tmp/roundcube-temp';

// This key is used for encrypting purposes, like storing of imap password
// in the session. For historical reasons it's called DES_key, but it's used
// with any configured cipher_method (see below).
// For the default cipher_method a required key length is 24 characters.
// des_key: removed -- now comes from .env / defaults

// Specifies the full path of the original HTTP request, either as a real path or
// $_SERVER field name. This might be useful when Roundcube runs behind a reverse
// proxy using a subpath. This is a path part of the URL, not the full URL!
// The reverse proxy config can specify a custom header (e.g. X-Forwarded-Path) containing
// the path under which Roundcube is exposed to the outside world (e.g. /rcube/).
// This header value is then available in PHP with $_SERVER['HTTP_X_FORWARDED_PATH'].
// By default the path comes from  'REDIRECT_SCRIPT_URL', 'SCRIPT_NAME' or 'REQUEST_URI',
// whichever is set (in this order).
$config['request_path'] = '/';

// ----------------------------------
// PLUGINS
// ----------------------------------
// List of active plugins (in plugins/ directory)
$config['plugins'] = ['archive', 'zipdownload', 'newmail_notifier', 'ident_switch'];

$config['no_save_sent_messages'] = false;

// Make use of the built-in spell checker.
$config['enable_spellcheck'] = true;

// Set the spell checking engine. Possible values:
// - 'googie'  - the default (also used for connecting to Nox Spell Server, see 'spellcheck_uri' setting)
// - 'pspell'  - requires the PHP Pspell module and aspell installed
// - 'enchant' - requires the PHP Enchant module
// - 'atd'     - install your own After the Deadline server or check with the people at https://www.afterthedeadline.com before using their API
// Since Google shut down their public spell checking service, the default settings
// connect to https://spell.roundcube.net which is a hosted service provided by Roundcube.
// You can connect to any other googie-compliant service by setting 'spellcheck_uri' accordingly.
$config['spellcheck_engine'] = 'pspell';

// Display remote resources (inline images, styles) in HTML messages. Default: 0.
// 0 - Never, always ask
// 1 - Allow from my contacts (all writeable addressbooks + collected senders and recipients)
// 2 - Always allow
// 3 - Allow from trusted senders only
$config['show_images'] = 2;

// compose html formatted messages by default
//  0 - never,
//  1 - always,
//  2 - on reply to HTML message,
//  3 - on forward or reply to HTML message
//  4 - always, except when replying to plain text message
$config['htmleditor'] = 1;

// Default interval for auto-refresh requests (in seconds)
// These are requests for system state updates e.g. checking for new messages, etc.
// Setting it to 0 disables the feature.
$config['refresh_interval'] = 300;

$config['zipdownload_selection'] = true;

// ---------------------------------------------------------------------
// ident_switch: preconfigured server settings for mirrored accounts.
// Without this, every identity you add falls back to Roundcube defaults
// instead of routing IMAP -> local dovecot mirror and SMTP -> Hostinger.
// '*' matches every domain since all mirrored accounts share the same
// IMAP/SMTP infrastructure.
// ---------------------------------------------------------------------
$config['ident_switch.preconfig'] = array (
  '*' => 
  array (
    'imap_host' => 'dovecot:143',
    'smtp_host' => 'ssl://smtp.hostinger.com:465',
    'user' => 'email',
    'readonly' => false,
  ),
);

include(__DIR__ . '/config.docker.inc.php');
$config['ident_switch_smtp_check'] = false;







// There is a Redis container in docker-compose.yml (roundcube-redis) that
// was never wired up here -- everything was falling back to MySQL for
// caching and sessions.
$config['redis_hosts'] = ['redis:6379'];
$config['session_storage'] = 'redis';

// Type of IMAP indexes cache.
$config['imap_cache'] = 'redis';

// Enables messages cache — caches the message list/headers locally so
// switching folders (Sent/Archive/Junk/etc.) doesn't re-fetch everything
// from IMAP every time.
$config['messages_cache'] = true;



$config['refresh_interval'] = 30;
