# Bridge — imagen de producción, mínima y sin root.
FROM python:3.12-slim

# No escribir .pyc, salida sin buffer (logs en vivo).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias primero (capa cacheable).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Solo los módulos del servicio (no scripts de exploración ni tests).
COPY config.py models.py negotiation.py otp.py fmcsa.py \
     tms_client.py tms_parser.py tms_service.py main.py ./

# Usuario sin privilegios.
RUN useradd --create-home --uid 10001 bridge
USER bridge

EXPOSE 8000

# Healthcheck: la sonda /health hace DEBUG_ECHO contra el TMS.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# Respeta $PORT si el PaaS lo inyecta (Railway/Render/Fly); 8000 por defecto (compose/local).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
