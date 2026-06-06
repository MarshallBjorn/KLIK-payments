# ── Stage 1: build statyku Vue (ciężkie, robione w CI — nie na VPS) ──
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# ── Stage 2: lekki nginx serwujący statyk ──
# Mock-bank UI woła backend po URL-u z localStorage (konfig runtime w SetupView),
# więc tu nie ma proxy — czysty statyk wystarczy.
FROM nginx:1.27-alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
