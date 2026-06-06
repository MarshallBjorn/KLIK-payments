# ── Stage 1: build statyku Vue (ciężkie, robione w CI — nie na VPS) ──
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# ── Stage 2: lekki nginx serwujący statyk + proxy /api → KLIK ──
FROM nginx:1.27-alpine
# Template envsubst'owany przez entrypoint nginx — KLIK_UPSTREAM nadpisywalny z compose.
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
ENV KLIK_UPSTREAM=web:8000
EXPOSE 80
