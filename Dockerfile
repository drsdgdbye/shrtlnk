# Стадия сборки: статический бинарник без CGO.
FROM golang:1.26 AS builder

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/shrtlnk .

# Пустой каталог-заготовка, чтобы в финальном образе /data принадлежал nonroot:
# новый named volume наследует владельца каталога образа, иначе запись БД из-под
# uid 65532 упрётся в permission denied.
RUN mkdir -p /out/data

# Финальная стадия: минимальный образ без компилятора и shell, запуск от nonroot.
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app

COPY --from=builder /out/shrtlnk /app/shrtlnk
COPY --from=builder /src/application.yaml /app/application.yaml
COPY --from=builder --chown=nonroot:nonroot /out/data/ /data/

USER nonroot:nonroot

EXPOSE 8080
VOLUME /data

ENTRYPOINT ["/app/shrtlnk"]
