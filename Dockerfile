FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy only what is needed to install and run the package.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip && \
    python -m pip install .

# Terraform Cloud Run Jobs override command/args, but this keeps local container runs valid.
CMD ["python", "-c", "print('aps-eb-oracle-integration image ready')"]
