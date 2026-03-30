FROM node:20-alpine AS base

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM base AS dev
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

FROM base AS build
COPY . .
RUN npm run build

FROM node:20-alpine AS production

WORKDIR /app

COPY --from=build /app/build ./build
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/package-lock.json ./package-lock.json
COPY --from=build /app/server.js ./server.js

RUN npm install --omit=dev

EXPOSE 80
ENV PORT=80
ENV HOST=0.0.0.0
ENV API_BASE_URL=http://finance-app:8777

CMD ["node", "server.js"]
