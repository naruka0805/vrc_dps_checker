# パーティ共有バックエンド(server.py)専用。オーバーレイ本体は含めない。
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# 状態をメモリに持つ設計なので、プロセスは必ず1つ。
# Cloud Run側も --max-instances=1 にしないと部屋が分裂する。
CMD ["python", "server.py"]
