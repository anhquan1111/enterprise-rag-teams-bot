# =============================================================================
# Dockerfile - Microsoft Teams Bot Service
# Base: python:3.11-slim (giống backend để đồng bộ môi trường)
# Port: 3978 (chuẩn Bot Framework)
# =============================================================================

FROM python:3.11-slim

WORKDIR /app

# Cài dependencies trước (layer cache hiệu quả hơn)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ source bot
COPY . .

EXPOSE 3978

CMD ["python", "app.py"]
