# UDP / SRT Throughput Tester — runs on Raspberry Pi (arm64) or x86_64
FROM python:3.11-slim-bookworm

# System packages: ffmpeg (with libsrt), iperf3, srt-tools (srt-live-transmit),
# iputils-ping for RTT tests.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        iperf3 \
        srt-tools \
        iputils-ping \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py
COPY tests /app/tests
COPY templates /app/templates
COPY static /app/static

ENV PORT=8080 \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]

EXPOSE 8080 5201/udp 9000/udp 9100/udp

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/app.py"]
