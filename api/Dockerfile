# Use the official Astral uv image with Python 3.12 preinstalled
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Set the working directory
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy the dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies using uv sync
RUN uv sync --frozen --no-dev

# Copy the rest of the application
COPY . .

# Expose port 8000
EXPOSE 8000

# Run FastAPI dev server with reloading
CMD ["uv", "run", "fastapi", "dev", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
