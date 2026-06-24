# Bridge — minimal, non-root production image.
FROM python:3.12-slim

# No .pyc files, unbuffered output (live logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cacheable layer).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Service modules only (no exploration scripts or tests).
COPY config.py models.py negotiation.py otp.py fmcsa.py \
     tms_client.py tms_parser.py tms_service.py main.py ./

# Unprivileged user.
RUN useradd --create-home --uid 10001 bridge
USER bridge

EXPOSE 8000

# Healthcheck: the /health probe runs DEBUG_ECHO against the TMS.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# Honor $PORT if the PaaS injects it (Railway/Render/Fly); default 8000 (compose/local).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
