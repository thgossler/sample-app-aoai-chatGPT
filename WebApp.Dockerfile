FROM node:22-slim AS frontend
RUN mkdir -p /home/node/app/frontend && chown -R node:node /home/node/app

WORKDIR /home/node/app/frontend
COPY --chown=node:node ./frontend/package*.json ./
USER node
RUN npm ci
COPY --chown=node:node ./frontend/ ./
COPY --chown=node:node ./static/ /home/node/app/static/
RUN NODE_OPTIONS=--max_old_space_size=8192 npm run build

FROM python:3.13-alpine
RUN apk add --no-cache --virtual .build-deps \
    build-base \
    libffi-dev \
    openssl-dev \
    && apk add --no-cache \
    curl

COPY requirements.txt /usr/src/app/
RUN pip install --no-cache-dir -r /usr/src/app/requirements.txt \
    && rm -rf /root/.cache \
    && apk del .build-deps

RUN adduser -D -h /usr/src/app appuser

COPY . /usr/src/app/
COPY --from=frontend /home/node/app/static /usr/src/app/static/
WORKDIR /usr/src/app
RUN chown -R appuser:appuser /usr/src/app

USER appuser
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:80/frontend_settings || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:80", "app:app"]
