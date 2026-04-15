# ---- Frontend build ----
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build

# ---- Runtime ----
FROM python:3.13-slim
WORKDIR /app

# Install Python deps
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy built frontend into static serving
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Create workspace directory
RUN mkdir -p workspace/context

EXPOSE 8000

# Start uvicorn — in production the FastAPI app also serves the static frontend
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
