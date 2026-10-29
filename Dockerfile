FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Code lives in /app; agents' file tools work in /work (mount your workspace there).
RUN useradd --create-home agent \
    && mkdir -p /work /app/trajectories \
    && chown agent /work /app/trajectories
USER agent
WORKDIR /work

EXPOSE 8000
CMD ["python", "/app/server.py"]
