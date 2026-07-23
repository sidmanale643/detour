FROM node:20-slim

WORKDIR /app

# Install pnpm globally
RUN npm install -g pnpm

# Copy package files
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./

# Install dependencies
RUN pnpm install

# Copy application files
COPY . .

# Expose Next.js port
EXPOSE 3000

# Start dev server
CMD ["pnpm", "run", "dev"]
