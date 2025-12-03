FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY model-btc/ ./model

ENV PYTHONPATH=/app

EXPOSE 8001

CMD ["uvicorn", "model.model_server:app", "--host", "0.0.0.0", "--port", "8001"]