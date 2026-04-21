#!/bin/bash
set -e

INTERVAL=${SLEEP_INTERVAL:-86400}

echo "🚀 Запуск сборщика статистики Telegram Gifts..."
while true; do
    python gift_stats_v2.py
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        echo "❌ Скрипт завершился с ошибкой (код $EXIT_CODE). Перезапуск через 60 секунд..."
        sleep 60
    else
        echo "✅ Сбор данных завершён успешно. Сон $INTERVAL секунд до следующего запуска..."
        sleep $INTERVAL
    fi
done
