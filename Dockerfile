FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install crunchyexporter-cli — pinned commit; update deliberately.
# Exact versions below match its own requirements.txt floor at this commit,
# pinned explicitly (rather than installed unpinned) so builds stay reproducible.
RUN git clone https://github.com/ruflas/crunchyexporter-cli.git /opt/crunchyexporter \
    && cd /opt/crunchyexporter && git checkout 1855e567ad1704a6655feedffcf76b1d77e5d690 \
    && pip install --no-cache-dir requests==2.31.0 click==8.1.7 pyyaml==6.0.1 rich==13.7.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY scripts/ scripts/

EXPOSE 8888
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888"]
