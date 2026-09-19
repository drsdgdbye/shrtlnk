// Command shrtlnk запускает Telegram-бота сокращения ссылок и HTTP-сервер редиректа.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"

	"yt_dw/config"
	"yt_dw/internal/redirect"
	"yt_dw/internal/shortener"
	"yt_dw/internal/telegram"
)

func main() {
	if err := run(); err != nil {
		slog.Error("приложение завершилось с ошибкой", "error", err)
		os.Exit(1)
	}
}

// run собирает зависимости и запускает оба компонента до сигнала завершения.
func run() error {
	cfg, err := config.NewConfig("application.yaml")
	if err != nil {
		return fmt.Errorf("загрузка конфигурации: %w", err)
	}
	logger := buildLogger(cfg.Log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	store, err := shortener.OpenStore(ctx, cfg.Store.DataFile)
	if err != nil {
		return fmt.Errorf("открытие хранилища: %w", err)
	}
	defer func() {
		if cerr := store.Close(); cerr != nil {
			logger.Error("закрытие хранилища", "error", cerr)
		}
	}()

	svc := shortener.NewService(store)

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	var (
		wg       sync.WaitGroup
		errMu    sync.Mutex
		firstErr error
	)
	launch := func(name string, fn func(context.Context) error) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := fn(ctx); err != nil {
				errMu.Lock()
				if firstErr == nil {
					firstErr = fmt.Errorf("%s: %w", name, err)
				}
				errMu.Unlock()
				cancel()
			}
		}()
	}

	launch("telegram", func(ctx context.Context) error {
		return telegram.Run(ctx, cfg.Bot.Token, svc, cfg.Bot.ShortURLBase, logger)
	})
	launch("redirect", func(ctx context.Context) error {
		return redirect.Run(ctx, cfg.HTTP.Addr, redirect.TLSConfig{
			CertFile: cfg.TLS.CertFile,
			KeyFile:  cfg.TLS.KeyFile,
		}, svc, logger)
	})

	wg.Wait()
	return firstErr
}

// buildLogger создаёт slog-логгер по уровню и формату из конфигурации.
func buildLogger(cfg config.LogConfig) *slog.Logger {
	var level slog.Level
	switch strings.ToLower(cfg.Level) {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	default:
		level = slog.LevelInfo
	}

	opts := &slog.HandlerOptions{Level: level}
	var handler slog.Handler
	if strings.EqualFold(cfg.Format, "json") {
		handler = slog.NewJSONHandler(os.Stdout, opts)
	} else {
		handler = slog.NewTextHandler(os.Stdout, opts)
	}
	return slog.New(handler)
}
