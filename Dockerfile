FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MERAKI_INVENTORY_HOST=0.0.0.0 \
    MERAKI_INVENTORY_PORT=8086

WORKDIR /app
COPY app.py index.html ./

USER 10001:10001
EXPOSE 8086
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8086/api/status', timeout=2)"

CMD ["python", "app.py"]
