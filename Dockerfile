FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash ca-certificates libatomic1 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir beautifulsoup4 pymupdf playwright pytest \
 && playwright install --with-deps chromium \
 && ln -sf python3 /usr/local/bin/python

COPY scripts/egress_gate.py /opt/factory/egress_gate.py

WORKDIR /work
CMD ["python3", "--version"]
