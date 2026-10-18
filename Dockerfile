FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEMORY_MESH_HOST=0.0.0.0 \
    MEMORY_MESH_PORT=8000 \
    FALKORDB_HOST=falkordb \
    FALKORDB_PORT=6380

EXPOSE 8000

CMD ["python3", "start.py"]
