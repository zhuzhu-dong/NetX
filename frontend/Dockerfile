# Immutable production image for the submission frontend.  Dependencies are
# installed from the lockfile during image construction; the runtime stage
# contains only the built static application and Nginx.
FROM node:20-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

FROM nginx:alpine AS runtime

COPY frontend/nginx.production.conf /etc/nginx/conf.d/default.conf
COPY frontend/healthcheck.sh /usr/local/bin/frontend-healthcheck
COPY --from=build /app/dist /usr/share/nginx/html
RUN chmod +x /usr/local/bin/frontend-healthcheck

EXPOSE 5173

# A successful TCP listener alone is insufficient: the served document must
# be a production bundle, not Vite's development entry point.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 CMD ["/usr/local/bin/frontend-healthcheck"]

CMD ["nginx", "-g", "daemon off;"]
