ARG MEDIA_BASE_IMAGE=debian:trixie-slim@sha256:7378260c0a098d27ac206225db5e7d92b8c7c84ea7afccc86eeb513756bbd392
FROM ${MEDIA_BASE_IMAGE} AS common
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nginx libnginx-mod-rtmp python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o www-data -g www-data /var/www/hls \
 && install -d /srv/sma/web
EXPOSE 8080 1935

FROM common AS checks
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

FROM common AS runtime
COPY nginx/nginx.conf.template /etc/nginx/sma-nginx.conf.template
COPY nginx/start.py /usr/local/lib/sma/start.py
COPY nginx/sma-proxy.conf /etc/nginx/sma-proxy.conf
COPY web/ /srv/sma/web/
RUN chmod -R a+rX /srv/sma/web \
 && chmod 644 /etc/nginx/sma-nginx.conf.template /etc/nginx/sma-proxy.conf
CMD ["python3", "/usr/local/lib/sma/start.py"]
