FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install crunchyexporter-cli — pinned commit; update deliberately
RUN git clone https://github.com/ruflas/crunchyexporter-cli.git /opt/crunchyexporter \
    && cd /opt/crunchyexporter && git checkout 1855e56 \
    && pip install --no-cache-dir requests click pyyaml rich

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/

EXPOSE 8888
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888"]
