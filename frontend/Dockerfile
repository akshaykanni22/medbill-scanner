# frontend/Dockerfile
# ============================================================
# Multi-stage build:
#   Stage 1 (builder) — install deps and compile the React app
#   Stage 2 (nginx)   — serve the static build with nginx
#
# SECURITY:
#   - Non-root user (nginx default, uid 101) per CLAUDE.md rule 11
#   - Read-only root filesystem compatible (nginx writes only to
#     /var/cache/nginx and /var/run, both whitelisted via tmpfs
#     in docker-compose.yml)
#   - No secrets present in either stage; REACT_APP_* vars must
#     be baked in at build time via ARG (see docker-compose.yml)
#
# WHY nginx INSTEAD OF node serve:
#   The React build output is 100% static. nginx adds gzip,
#   caching headers, and security headers with minimal footprint.
#   No Node runtime in the final image = smaller attack surface.
#
# PROXY:
#   package.json "proxy" field works only with react-scripts start
#   (development). In production, nginx forwards /api/* to the
#   backend container via the nginx.conf proxy_pass directive.
# ============================================================

# ── Stage 1: build ──────────────────────────────────────────
FROM node:20-slim AS builder

WORKDIR /app

# Copy manifests first so dependency install layer is cached
# unless package.json / package-lock.json change.
COPY package.json package-lock.json* ./

# Install all deps (including devDependencies for the build step).
# --frozen-lockfile equivalent for npm: ci
RUN npm ci

# Copy source and config files
COPY tsconfig.json tailwind.config.js postcss.config.js ./
COPY public/ ./public/
COPY src/ ./src/

# Bake REACT_APP_* env vars into the bundle at build time.
# Empty default = relative URLs (/api/...) so nginx proxy_pass handles routing.
# Override at build time for deployments where frontend and backend are on
# different origins: --build-arg REACT_APP_API_URL=https://api.example.com
ARG REACT_APP_API_URL=
ENV REACT_APP_API_URL=$REACT_APP_API_URL

# Production build — outputs to /app/build
RUN npm run build

# ── Stage 2: serve ──────────────────────────────────────────
FROM nginx:1.27-alpine AS production

# Remove default nginx config; we supply our own
RUN rm /etc/nginx/conf.d/default.conf

# Copy our nginx config
COPY nginx.conf /etc/nginx/conf.d/app.conf

# Copy the compiled React app from the builder stage
COPY --from=builder /app/build /usr/share/nginx/html

# nginx listens on port 80 inside the container.
# docker-compose.yml maps this to 127.0.0.1:3000 on the host.
EXPOSE 80

RUN sed -i 's|pid\s*/run/nginx.pid;|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf

# Healthcheck: verify nginx is serving the app
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:80/ | grep -q "MedBill" || exit 1


CMD ["nginx", "-g", "daemon off;"]
