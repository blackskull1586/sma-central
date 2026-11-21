ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:48d884813cf44e927cf64c5529dd630dd3b18338c6dc727991eef9b71e934a20
FROM ${PYTHON_IMAGE} AS dependencies
COPY vendor/portal/ /wheelhouse/
COPY backend/requirements.txt /requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --index-url https://pypi.org/simple --require-hashes \
        --ignore-installed --prefix=/install \
        -r /wheelhouse/requirements.lock -r /requirements.txt

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=dependencies /install/ /usr/local/
WORKDIR /app
RUN groupadd --gid 10001 sma \
 && useradd --uid 10001 --gid 10001 --no-create-home sma
COPY backend/ /app/
RUN chmod -R a+rX /app
USER 10001:10001
EXPOSE 8000
CMD ["/bin/sh", "/app/start.sh"]
