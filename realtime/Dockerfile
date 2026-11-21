ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:48d884813cf44e927cf64c5529dd630dd3b18338c6dc727991eef9b71e934a20
FROM ${PYTHON_IMAGE} AS dependencies
COPY vendor/gateway/ /wheelhouse/
COPY realtime/requirements.txt /requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --index-url https://pypi.org/simple --require-hashes \
        --ignore-installed --prefix=/install \
        -r /wheelhouse/requirements.lock -r /requirements.txt

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=dependencies /install/ /usr/local/
WORKDIR /app
COPY realtime/gateway.py /app/gateway.py
RUN chmod 644 /app/gateway.py
USER 10001:10001
CMD ["python", "-u", "/app/gateway.py"]
