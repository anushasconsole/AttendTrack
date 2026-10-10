FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake \
    build-essential \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Install numpy FIRST (pinned) so dlib's bindings link against a known-good ABI
RUN pip install --no-cache-dir "numpy==1.26.4"

# Install dlib next (takes time to compile), then face_recognition, then the rest
RUN pip install --no-cache-dir dlib==19.24.1
RUN pip install --no-cache-dir face_recognition==1.3.0
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    "uvicorn[standard]==0.29.0" \
    python-dotenv==1.0.1 \
    "pydantic==2.7.1" \
    "pydantic-core==2.18.2" \
    anyio==4.3.0 \
    httpx==0.27.0 \
    starlette==0.37.2 \
    psycopg2-binary==2.9.9 \
    "Pillow>=10.4.0"

# WhatsApp overtime alerts — pure-Python, installs fast, own layer so it
# doesn't bust the cache for the slow dlib/face_recognition layers above
RUN pip install --no-cache-dir twilio==9.0.4

# New intelligence layer — ML + scheduler + Excel reports
RUN pip install --no-cache-dir \
    apscheduler==3.10.4 \
    scikit-learn==1.4.2 \
    joblib==1.4.2 \
    pandas==2.2.2 \
    openpyxl==3.1.2

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
