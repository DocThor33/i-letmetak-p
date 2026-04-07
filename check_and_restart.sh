#!/bin/bash

# Muhasebe uygulaması kontrol ve yeniden başlatma scripti
APP_DIR="/Users/erenseyis/Desktop/deneme iş"
LOG_FILE="$APP_DIR/restart_log.txt"

echo "$(date): Kontrol başlatıldı" >> "$LOG_FILE"

# Streamlit process'ini kontrol et
if ! pgrep -f "streamlit.*app.py" > /dev/null; then
    echo "$(date): Uygulama çalışmıyor, yeniden başlatılıyor" >> "$LOG_FILE"
    cd "$APP_DIR"
    nohup python3 -m streamlit run app.py --server.address 0.0.0.0 --server.port 8503 >> "$LOG_FILE" 2>&1 &
    echo "$(date): Uygulama yeniden başlatıldı (PID: $!)" >> "$LOG_FILE"
else
    echo "$(date): Uygulama çalışıyor" >> "$LOG_FILE"
fi