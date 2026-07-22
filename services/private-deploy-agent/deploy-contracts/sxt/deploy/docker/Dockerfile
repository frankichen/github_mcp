# syntax=docker/dockerfile:1.7

ARG GO_IMAGE=golang:1.26.4-alpine3.23
ARG NODE_IMAGE=node:22-alpine3.23
ARG NGINX_IMAGE=nginx:1.29-alpine3.23
ARG ALPINE_IMAGE=alpine:3.23
ARG GOOSE_VERSION=v3.22.1

FROM ${GO_IMAGE} AS goose-builder
ARG GOOSE_VERSION
WORKDIR /src
RUN apk add --no-cache ca-certificates git \
    && GOBIN=/out go install -tags='no_clickhouse no_libsql no_mssql no_mysql no_sqlite3 no_vertica no_ydb' github.com/pressly/goose/v3/cmd/goose@${GOOSE_VERSION}

FROM ${GO_IMAGE} AS go-builder
WORKDIR /src
RUN apk add --no-cache ca-certificates git
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/lenshub-api ./cmd/api \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/lenshub-worker ./cmd/worker \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/lenshub-scheduler ./cmd/scheduler \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/lenshub-admin-bootstrap ./cmd/admin-bootstrap

FROM scratch AS binary-export
COPY --from=go-builder /out/ /bin/
COPY --from=goose-builder /out/goose /bin/goose
COPY db/migrations /db/migrations
COPY i18n/app /i18n/app

FROM ${ALPINE_IMAGE} AS runtime
RUN addgroup -S lenshub \
    && adduser -S -D -H -h /app -G lenshub lenshub \
    && apk add --no-cache ca-certificates tzdata
WORKDIR /app
USER lenshub

FROM ${NODE_IMAGE} AS web-builder
WORKDIR /web
COPY h5/lenshub-admin/package*.json ./
RUN npm ci
COPY h5/lenshub-admin/ ./
RUN chmod +x node_modules/.bin/* && node_modules/.bin/vite build

FROM ${NGINX_IMAGE} AS web-static
COPY deploy/nginx/lenshub-test.conf /etc/nginx/conf.d/default.conf
COPY h5/lenshub-app-simulator /usr/share/nginx/html
COPY h5/lenshub-app-simulator /usr/share/nginx/html/app
COPY h5/lenshub-flow-tester /usr/share/nginx/html/tester
COPY h5/lenshub-api-docs /usr/share/nginx/html/docs
COPY api/openapi /usr/share/nginx/html/openapi
COPY --from=web-builder /web/dist /usr/share/nginx/html/admin

FROM ${NGINX_IMAGE} AS gateway
COPY deploy/nginx/lenshub-gateway.conf /etc/nginx/conf.d/default.conf
