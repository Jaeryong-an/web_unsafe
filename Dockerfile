# ベース: Debian系 python:3.10-slim
FROM python:3.10-slim

# 必要Systemライブラリのインストール（t64対策含む）
RUN set -eux \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    wget unzip curl gnupg ca-certificates fonts-liberation \
    libasound2 libatk-bridge2.0-0 libcups2 libdbus-1-3 \
    libnspr4 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
    xdg-utils libgdk-pixbuf-2.0-0 \
 && if apt-cache show libatk1.0-0 >/dev/null 2>&1; then \
      apt-get install -y --no-install-recommends libatk1.0-0; \
    else \
      apt-get install -y --no-install-recommends libatk1.0-0t64; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# Google Chrome の公式リポジトリ登録（apt-key非推奨対応）
RUN set -eux \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

# Python依存パッケージ
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 作業ディレクトリとソース
WORKDIR /app
COPY . .

# Entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 7860
CMD ["/app/entrypoint.sh"]
