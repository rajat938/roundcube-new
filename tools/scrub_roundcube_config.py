"""
Remove secrets and unsafe settings from Roundcube's config.inc.php.

  python3 tools/scrub_roundcube_config.py www/config/config.inc.php

Removes:
  $config['db_dsnw']            DB password -- set from .env via config.docker.inc.php
  $config['des_key']            encryption key -- now ROUNDCUBE_DES_KEY in .env
  $config['smtp_conn_options']  turned OFF certificate checks for Hostinger SMTP
  $config['imap_conn_options']  same, for IMAP

Copy the des_key value into .env (ROUNDCUBE_DES_KEY=...) BEFORE running this
if you want to keep it. A backup is written next to the file (*.bak -- it
still contains the secrets; delete it once Roundcube works).
"""
import re
import shutil
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "www/config/config.inc.php"
src = open(path, encoding="utf-8").read()

patterns = {
    "db_dsnw": r"^\s*\$config\['db_dsnw'\]\s*=\s*'[^']*'\s*;[^\n]*\n",
    "des_key": r"^\s*\$config\['des_key'\]\s*=\s*'[^']*'\s*;[^\n]*\n",
    "smtp_conn_options": r"^[ \t]*\$config\['smtp_conn_options'\]\s*=\s*(?:array\s*\(|\[).*?(?:\)|\])\s*;[^\n]*\n",
    "imap_conn_options": r"^[ \t]*\$config\['imap_conn_options'\]\s*=\s*(?:array\s*\(|\[).*?(?:\)|\])\s*;[^\n]*\n",
}
out, removed = src, []
for name, pat in patterns.items():
    m = re.search(pat, out, flags=re.M | re.S)
    if m:
        if name == "des_key":
            key = re.search(r"'([^']*)'\s*;", m.group(0).split("=", 1)[1]).group(1)
            print(f"des_key found -- make sure .env has:  ROUNDCUBE_DES_KEY={key}")
        out = out[:m.start()] + f"// {name}: removed -- now comes from .env / defaults\n" + out[m.end():]
        removed.append(name)

if not removed:
    print("Nothing to remove -- file already clean.")
    sys.exit(0)
if "config.docker.inc.php" not in out:
    sys.exit("Refusing: this file does not include config.docker.inc.php, so the DB settings "
             "would be lost. Nothing changed.")
shutil.copy2(path, path + ".bak")
open(path, "w", encoding="utf-8").write(out)
print("Removed:", ", ".join(removed))
print(f"Backup: {path}.bak  (contains the old secrets -- delete it after checking)")
