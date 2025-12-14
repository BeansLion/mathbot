# MathTrain Bot

Тренажёр устного умножения в Telegram.

## Команды
- /start — меню + выбор уровня
- /hint — подсказка к текущему примеру
- /answer — показать ответ + кнопка "Новый пример"
- /theory — теория
- /stats — статистика
- /theme — темы (есть "Без оформления")
- /stop — остановить тренировку

## Локальный запуск
1) Создай .env:
   BOT_TOKEN=...
   DB_PATH=mathtrain.db
2) Установи зависимости:
   pip install -r requirements.txt
3) Запусти:
   python bot.py

## Railway
1) Добавь Volume и смонтируй в /data
2) Variables:
   BOT_TOKEN=...
   DB_PATH=/data/mathtrain.db
3) Deploy from GitHub
