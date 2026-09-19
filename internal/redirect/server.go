// Package redirect обслуживает HTTP-редирект по короткому коду и health-check.
package redirect

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"yt_dw/internal/shortener"
)

// Таймауты HTTP-сервера и graceful shutdown.
const (
	readHeaderTimeout = 5 * time.Second
	readTimeout       = 10 * time.Second
	writeTimeout      = 10 * time.Second
	idleTimeout       = 60 * time.Second
	shutdownTimeout   = 10 * time.Second
)

// Resolver находит ссылку по короткому коду.
type Resolver interface {
	Resolve(ctx context.Context, code string) (shortener.Link, error)
}

// TLSConfig описывает пути к сертификату и ключу. Оба поля должны быть либо пустыми,
// либо заполненными; пару валидирует пакет config.
type TLSConfig struct {
	CertFile string
	KeyFile  string
}

// NewHandler собирает HTTP-обработчик: GET /{code} — редирект, GET /healthz — health.
func NewHandler(resolver Resolver, logger *slog.Logger) http.Handler {
	if logger == nil {
		logger = slog.Default()
	}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeText(w, http.StatusOK, "ok")
	})

	mux.HandleFunc("GET /{code}", func(w http.ResponseWriter, r *http.Request) {
		code := r.PathValue("code")
		link, err := resolver.Resolve(r.Context(), code)
		if err != nil {
			if !errors.Is(err, shortener.ErrNotFound) {
				logger.Error("не удалось разрешить короткий код", "code", code, "error", err)
				writeText(w, http.StatusInternalServerError, "internal error")
				return
			}
			writeText(w, http.StatusNotFound, "not found")
			return
		}
		http.Redirect(w, r, link.URL, http.StatusFound)
	})

	return mux
}

// Run поднимает HTTP-сервер на addr и блокируется до отмены ctx,
// после чего выполняет graceful shutdown. TLS включается при заполненной паре файлов.
func Run(ctx context.Context, addr string, tlsCfg TLSConfig, resolver Resolver, logger *slog.Logger) error {
	srv := &http.Server{
		Addr:              addr,
		Handler:           NewHandler(resolver, logger),
		ReadHeaderTimeout: readHeaderTimeout,
		ReadTimeout:       readTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
	}

	errCh := make(chan error, 1)
	go func() {
		var err error
		if tlsCfg.CertFile != "" && tlsCfg.KeyFile != "" {
			err = srv.ListenAndServeTLS(tlsCfg.CertFile, tlsCfg.KeyFile)
		} else {
			err = srv.ListenAndServe()
		}
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
			return
		}
		errCh <- nil
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			return fmt.Errorf("остановка HTTP-сервера: %w", err)
		}
		return nil
	case err := <-errCh:
		if err != nil {
			return fmt.Errorf("HTTP-сервер: %w", err)
		}
		return nil
	}
}

// writeText пишет текстовый ответ с заданным статусом.
func writeText(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	if _, err := w.Write([]byte(body)); err != nil {
		return
	}
}
