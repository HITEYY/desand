FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
# Headless Chromium + system libs, so agents can use the browser tools inside the image.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R 755 /ms-playwright
COPY . .

# Code lives in /app; agents' file tools work in /work (mount your workspace there).
RUN useradd --create-home agent \
    && mkdir -p /work /app/trajectories \
    && chown agent /work /app/trajectories
USER agent
WORKDIR /work

EXPOSE 8000
CMD ["python", "/app/server.py"]
