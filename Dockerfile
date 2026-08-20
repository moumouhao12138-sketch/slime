ARG PYTHON_IMAGE=python:3.13-slim
ARG DOCKER_CLI_IMAGE=docker:29-cli

FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Slime Cairn" \
      org.opencontainers.image.description="Persistent multi-project agent exploration runtime" \
      org.opencontainers.image.source="https://github.com/moumouhao12138-sketch/slime"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    SLIME_PROJECT_ROOT=/app

WORKDIR /app

# The dispatcher talks to the host Docker Engine through its mounted socket.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY pyproject.toml README.md dispatch.json ./
COPY src ./src

RUN apt-get update -o Acquire::Retries=5 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=5 nodejs \
    && node --version \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir . \
    && python -m compileall -q /app/src

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "slime_cairn.server.api:app", "--host", "0.0.0.0", "--port", "8000"]
