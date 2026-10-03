FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py planner.py ./
COPY static ./static

EXPOSE 8000

# Flask 内置服务器即可满足本演示体量；生产可换 gunicorn。
CMD ["python", "app.py"]
