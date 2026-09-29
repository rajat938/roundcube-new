# One image, two services (see docker-compose.yml):
#   sync-pull -> python3 /app/pull.py
#   sync-push -> python3 /app/push.py
# No imapsync, no EOL Debian image, no archive.debian.org hacks.
FROM python:3.12-slim

RUN pip install --no-cache-dir "imapclient==4.1.0" "PyMySQL==1.2.3"

WORKDIR /app
COPY common.py pull.py push.py check_folder.py find_duplicates.py /app/

ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python3", "/app/pull.py"]
