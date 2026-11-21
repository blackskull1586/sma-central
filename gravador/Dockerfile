FROM python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg postgresql-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/recorder
COPY recorder.py .
COPY config.example.json config.json
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["python3", "/opt/recorder/recorder.py", "--config", "/opt/recorder/config.json"]
