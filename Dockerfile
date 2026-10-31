FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY dashboard ./dashboard
COPY .streamlit ./.streamlit

RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app/data
USER app

CMD ["ez-money", "run"]
