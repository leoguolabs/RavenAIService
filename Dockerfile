FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    gcc \
    g++ \
    git \
    gzip \
    jq \
    libarchive-tools \
    libreoffice-calc \
    openssh-client \
    python3-dev \
    ripgrep \
    tar \
    unar \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/install-antigravity.py /tmp/install-antigravity.py
RUN python /tmp/install-antigravity.py && rm /tmp/install-antigravity.py

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir \
      --timeout 300 \
      --retries 5 \
      --trusted-host pypi.org \
      --trusted-host pypi.python.org \
      --trusted-host files.pythonhosted.org \
      -r requirements.txt

COPY . .

RUN if [ -f /app/bin/tool_log_decompress ]; then \
      cp /app/bin/tool_log_decompress /usr/local/bin/tool_log_decompress \
      && chmod +x /usr/local/bin/tool_log_decompress; \
    else \
      echo "WARN: bin/tool_log_decompress not found; protocol stack decompression tasks will require it at runtime."; \
    fi \
    && mkdir -p /app/logs /app/temp/logs /app/temp/downloads /app/data /app/uploads/packages \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=30s --start-period=10s --retries=3 \
  CMD ["curl", "-f", "http://localhost:8085/health"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]
