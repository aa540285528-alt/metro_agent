FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[dev]"
EXPOSE 8000
CMD ["uvicorn", "metro_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
