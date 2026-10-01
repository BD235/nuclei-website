FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pin a known-good nuclei release. Check https://github.com/projectdiscovery/nuclei/releases
# for the latest version and bump this if you want a newer one.
ARG NUCLEI_VERSION=3.3.7
RUN curl -L -o /tmp/nuclei.zip \
    https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VERSION}/nuclei_${NUCLEI_VERSION}_linux_amd64.zip \
    && unzip /tmp/nuclei.zip -d /usr/local/bin \
    && rm /tmp/nuclei.zip \
    && chmod +x /usr/local/bin/nuclei

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Pull the current template set at build time so scans work offline afterwards.
RUN nuclei -update-templates -silent || true

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
