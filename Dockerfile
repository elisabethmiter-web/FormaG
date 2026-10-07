FROM python:3.12-slim
# LibreOffice lets clients see Word/Excel/PowerPoint files on the signing page exactly as laid out.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui \
      libreoffice-impress-nogui fonts-dejavu fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 --timeout 120 -b 0.0.0.0:${PORT} app:app"]
