FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
# Where the browser reaches the agent. Override per environment at build time.
ARG VITE_AGENT_URL=http://localhost:7860
ENV VITE_AGENT_URL=${VITE_AGENT_URL}
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
