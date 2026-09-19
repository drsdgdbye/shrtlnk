package config

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const validConfigYAML = "log:\n  level: debug\n  format: json\n"

// envKeys — переменные окружения, которыми управляет пакет.
var envKeys = []string{
	"BOT_TOKEN",
	"SHORT_URL_BASE",
	"HTTP_ADDR",
	"DATA_FILE",
	"TLS_CERT_FILE",
	"TLS_KEY_FILE",
	"LOG_LEVEL",
	"LOG_FORMAT",
}

// writeConfig создаёт временный YAML-файл с заданным содержимым.
func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "application.yaml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("запись конфигурации: %v", err)
	}
	return path
}

// setEnv сбрасывает все переменные пакета и выставляет переданные значения.
func setEnv(t *testing.T, values map[string]string) {
	t.Helper()
	for _, key := range envKeys {
		t.Setenv(key, "")
	}
	for key, value := range values {
		t.Setenv(key, value)
	}
}

func TestNewConfig(t *testing.T) {
	t.Run("нет BOT_TOKEN", func(t *testing.T) {
		setEnv(t, map[string]string{"SHORT_URL_BASE": "https://shrt.example"})
		_, err := NewConfig(writeConfig(t, validConfigYAML))
		if !errors.Is(err, ErrMissingBotToken) {
			t.Fatalf("ошибка = %v, ожидалась ErrMissingBotToken", err)
		}
	})

	t.Run("нет SHORT_URL_BASE", func(t *testing.T) {
		setEnv(t, map[string]string{"BOT_TOKEN": "123:secret"})
		_, err := NewConfig(writeConfig(t, validConfigYAML))
		if !errors.Is(err, ErrMissingBase) {
			t.Fatalf("ошибка = %v, ожидалась ErrMissingBase", err)
		}
	})

	t.Run("некорректный SHORT_URL_BASE", func(t *testing.T) {
		setEnv(t, map[string]string{
			"BOT_TOKEN":      "123:secret",
			"SHORT_URL_BASE": "ftp://shrt.example",
		})
		_, err := NewConfig(writeConfig(t, validConfigYAML))
		if !errors.Is(err, ErrInvalidBase) {
			t.Fatalf("ошибка = %v, ожидалась ErrInvalidBase", err)
		}
	})

	t.Run("неполная TLS-пара", func(t *testing.T) {
		setEnv(t, map[string]string{
			"BOT_TOKEN":      "123:secret",
			"SHORT_URL_BASE": "https://shrt.example",
			"TLS_CERT_FILE":  "/certs/cert.pem",
		})
		_, err := NewConfig(writeConfig(t, validConfigYAML))
		if !errors.Is(err, ErrIncompleteTLS) {
			t.Fatalf("ошибка = %v, ожидалась ErrIncompleteTLS", err)
		}
	})

	t.Run("успешно и env важнее yaml", func(t *testing.T) {
		setEnv(t, map[string]string{
			"BOT_TOKEN":      "123:secret",
			"SHORT_URL_BASE": "https://shrt.example/",
			"HTTP_ADDR":      ":9090",
			"DATA_FILE":      "/data/links.db",
			"TLS_CERT_FILE":  "/certs/cert.pem",
			"TLS_KEY_FILE":   "/certs/key.pem",
			"LOG_LEVEL":      "error",
			"LOG_FORMAT":     "text",
		})
		cfg, err := NewConfig(writeConfig(t, validConfigYAML))
		if err != nil {
			t.Fatalf("NewConfig: %v", err)
		}
		if cfg.Bot.Token != "123:secret" {
			t.Errorf("Token = %q", cfg.Bot.Token)
		}
		if cfg.Bot.ShortURLBase != "https://shrt.example" {
			t.Errorf("ShortURLBase = %q, ожидался хвостовой слэш срезан", cfg.Bot.ShortURLBase)
		}
		if cfg.HTTP.Addr != ":9090" {
			t.Errorf("Addr = %q", cfg.HTTP.Addr)
		}
		if cfg.Store.DataFile != "/data/links.db" {
			t.Errorf("DataFile = %q", cfg.Store.DataFile)
		}
		if cfg.TLS.CertFile != "/certs/cert.pem" || cfg.TLS.KeyFile != "/certs/key.pem" {
			t.Errorf("TLS = %+v", cfg.TLS)
		}
		if cfg.Log.Level != "error" || cfg.Log.Format != "text" {
			t.Errorf("Log = %+v, env должен переопределять yaml", cfg.Log)
		}
	})

	t.Run("значения по умолчанию из yaml", func(t *testing.T) {
		setEnv(t, map[string]string{
			"BOT_TOKEN":      "123:secret",
			"SHORT_URL_BASE": "https://shrt.example",
		})
		cfg, err := NewConfig(writeConfig(t, validConfigYAML))
		if err != nil {
			t.Fatalf("NewConfig: %v", err)
		}
		if cfg.HTTP.Addr != ":8080" {
			t.Errorf("Addr = %q, ожидался :8080", cfg.HTTP.Addr)
		}
		if cfg.Store.DataFile != "./data/links.db" {
			t.Errorf("DataFile = %q, ожидался ./data/links.db", cfg.Store.DataFile)
		}
		if cfg.Log.Level != "debug" || cfg.Log.Format != "json" {
			t.Errorf("Log = %+v, ожидались значения из yaml", cfg.Log)
		}
	})

	t.Run("токен не попадает в текст ошибки", func(t *testing.T) {
		const secret = "123456:SUPER-SECRET-TOKEN"
		setEnv(t, map[string]string{
			"BOT_TOKEN":      secret,
			"SHORT_URL_BASE": "ftp://shrt.example",
		})
		_, err := NewConfig(writeConfig(t, validConfigYAML))
		if err == nil {
			t.Fatal("ожидалась ошибка")
		}
		if strings.Contains(err.Error(), secret) {
			t.Errorf("токен найден в тексте ошибки: %v", err)
		}
	})
}
