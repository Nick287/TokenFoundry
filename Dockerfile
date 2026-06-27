# Token Foundry — single image: builds the React portal, then serves it from
# FastAPI alongside the API (one container, no nginx).

# --- Stage 1: build the React portal -> static assets ---
FROM node:22-alpine AS portal-build
WORKDIR /portal
COPY portal/package.json portal/package-lock.json* ./
RUN npm install
COPY portal/ ./
RUN npm run build

# --- Stage 2: Python backend + baked-in static frontend ---
FROM python:3.12-slim
WORKDIR /app

# Install backend deps first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
COPY worker ./worker

# Bake the built SPA into ./static — app.main mounts it if present
COPY --from=portal-build /portal/dist ./static

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
