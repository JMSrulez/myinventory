FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MERAKI_INVENTORY_HOST=0.0.0.0 \
    MERAKI_INVENTORY_PORT=8086 \
    MERAKI_INVENTORY_DATABASE=/data/inventory.sqlite3

WORKDIR /app
COPY app.py index.html inventory.html proxmox.html ./
RUN mkdir -p /data && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8086
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8086/api/status', timeout=2)"

CMD ["python", "app.py"]
