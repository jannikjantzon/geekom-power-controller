FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends samba-common-bin \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY powerctl.py /app/powerctl.py

RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin powerctl
USER powerctl

ENTRYPOINT ["python3", "/app/powerctl.py"]
