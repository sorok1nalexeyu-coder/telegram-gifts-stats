FROM python:3.11-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Установка системных зависимостей для matplotlib
RUN apt-get update && apt-get install -y \
    libpng-dev \
    libfreetype6-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Копирование зависимостей и установка
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY . .

# Создаём папку для данных (будет переопределена volume)
RUN mkdir -p /app/data

# Команда запуска
CMD ["python", "gift_stats_v2.py"]

