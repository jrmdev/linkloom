FROM python:3.13-alpine3.21 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV_PATH=/opt/venv

RUN python -m venv "$VENV_PATH"
ENV PATH="$VENV_PATH/bin:$PATH"

RUN apk add --no-cache --virtual .build-deps \
        gcc \
        musl-dev \
        libxml2-dev \
        libxslt-dev

RUN pip install \
        APScheduler==3.11.0 \
        beautifulsoup4==4.13.4 \
        Flask==3.1.1 \
        Flask-Login==0.6.3 \
        Flask-Migrate==4.1.0 \
        Flask-SQLAlchemy==3.1.1 \
        httpx==0.28.1 \
        lxml==5.4.0 \
        python-dateutil==2.9.0.post0 \
        rapidfuzz==3.14.1 \
        trafilatura==1.12.2

RUN apk del .build-deps

FROM python:3.13-alpine3.21

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_URL="sqlite:////data/linkloom.db"

RUN apk add --no-cache \
        libxml2 \
        libxslt

WORKDIR /app
RUN mkdir -p /data

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY templates ./templates
COPY static ./static
COPY run.py ./run.py

EXPOSE 5000

CMD ["python", "-m", "flask", "--app", "run:app", "run", "--host=0.0.0.0", "--port=8072"]
