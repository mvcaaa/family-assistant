# Ссылки на vault family-assistant в 1Password. Значений тут нет.
# Собрать локальный файл: op inject -i deploy/env.op.tpl -o .env && chmod 600 .env

TELEGRAM_BOT_TOKEN=op://family-assistant/telegram/password
ASSISTANT_BACKEND=bot
GROK_BOT_WEBHOOK_URL=op://family-assistant/grok-bot/url
GROK_BOT_WEBHOOK_KEY=op://family-assistant/grok-bot/password
GATEWAY_PUBLIC_URL=op://family-assistant/gateway/public-url
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8787
PASS_TTL=900
XAI_API_KEY=op://family-assistant/xai/password
XAI_MODEL=grok-4.7
ASSISTANT_NAME=Aino
ALLOWED_PHONES=op://family-assistant/gateway/allowed-phones
OWNER_PHONE=op://family-assistant/gateway/owner-phone
DATA_DIR=/data
ENABLE_WEB_SEARCH=1
VOICE=0
TTS_VOICE=ara
TTS_LANGUAGE=ru
STT_LANGUAGE=ru
APPROVAL_TTL=600
REVIEW_DAYS=7
