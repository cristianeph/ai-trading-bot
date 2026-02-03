FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY bot-futures ./bot

ENV PYTHONPATH=/app

# Variables de entorno para producción se pasan en docker-compose
CMD ["python", "-m", "bot.bot_futures"]