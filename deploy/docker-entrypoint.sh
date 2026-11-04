#!/bin/sh
set -eu
mkdir -p /data
chown familybot:familybot /data
if [ -f /env/family-assistant.env ]; then
    exec setpriv --reuid=familybot --regid=familybot --init-groups -- python3 -m family_assistant --env /env/family-assistant.env
fi
exec setpriv --reuid=familybot --regid=familybot --init-groups -- python3 -m family_assistant
