# Build the AgentScope web UI (React + Vite), then serve it with nginx.
# Source: examples/web_ui/frontend from agentscope v2.0.10, copied into
# this repo so upstream churn cannot break us (see docs/数据结构.md).
#
# --ignore-scripts: pnpm >=10.13 hard-errors on non-allowed build scripts
# (unrs-resolver etc.). All native deps here ship prebuilt platform
# packages as optionalDependencies, so skipping postinstall is safe and
# sidesteps the allowlist machinery entirely.
FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable

COPY frontend/package.json ./
RUN pnpm install --ignore-scripts

COPY frontend/ ./
RUN pnpm build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
