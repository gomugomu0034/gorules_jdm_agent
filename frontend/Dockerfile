FROM node:20-alpine

WORKDIR /app

# Install dependencies
COPY package.json package-lock.json* ./
RUN npm install

# The Next.js code is mounted via docker-compose volumes during local dev