FROM python:3.11-slim

WORKDIR /app

# Instala dependências do sistema para reportlab e psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8080", "--timeout", "300", "--workers", "1"]
