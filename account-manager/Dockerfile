FROM python:3.12-slim
RUN pip install --no-cache-dir "flask==3.1.3" "werkzeug==3.1.8" "waitress==3.0.2" "PyMySQL==1.2.3"
WORKDIR /app
COPY app.py /app/app.py
ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python3", "/app/app.py"]
