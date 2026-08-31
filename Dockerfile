FROM python:3.12-slim
WORKDIR /app
ARG TORCH_VERSION=2.12.0
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch==${TORCH_VERSION}+cpu"
COPY . .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 120 "."
EXPOSE 8000
CMD ["uvicorn", "metro_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
