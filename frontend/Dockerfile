# Stage 1: Build the SvelteKit application
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./

RUN npm install

COPY . .

RUN npm run build

# Stage 2: Run the application with Node.js
FROM node:20-alpine AS final

WORKDIR /app

# Copy built assets
COPY --from=build /app/build ./build
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/package-lock.json ./package-lock.json
COPY --from=build /app/server.js ./server.js

# Install production dependencies only
RUN npm install --omit=dev

# Expose port 80 (to match docker-compose mapping)
EXPOSE 80
ENV PORT=80
ENV HOST=0.0.0.0

# Default API_BASE_URL (can be overridden by docker-compose)
ENV API_BASE_URL=http://finance-app:8777

# Start Custom Node server (handles WS proxy + SvelteKit)
CMD ["node", "server.js"]
