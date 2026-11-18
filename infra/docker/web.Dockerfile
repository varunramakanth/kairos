# Kairos web UI, Next.js standalone output (ADR-017).
# Build from the repository root:  docker build -f infra/docker/web.Dockerfile .
FROM node:24-slim AS build
RUN corepack enable
WORKDIR /web
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web ./
# Same origin as the API behind CloudFront.
ENV NEXT_PUBLIC_KAIROS_API_URL=/api \
    NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM node:24-slim
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /web
COPY --from=build /web/.next/standalone ./
COPY --from=build /web/.next/static ./.next/static
COPY --from=build /web/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
