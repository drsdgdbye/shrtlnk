// Package config загружает настройки приложения из application.yaml и переменных окружения.
package config

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

// Значения по умолчанию для необязательных настроек.
const (
	defaultHTTPAddr = ":8080"
	defaultDataFile = "./data/links.db"
	defaultLogLevel = "info"
	defaultLogFmt   = "text"
)

// Config объединяет все настройки приложения.
type Config struct {
	Log   LogConfig `yaml:"log"`
	Bot   BotConfig
	HTTP  HTTPConfig
	Store StoreConfig
	TLS   TLSConfig
}

// LogConfig описывает настройки логирования.
type LogConfig struct {
	Level  string `yaml:"level"`
	Format string `yaml:"format"`
}

// BotConfig описывает настройки Telegram-бота.
type BotConfig struct {
	Token        string
	ShortURLBase string
}

// HTTPConfig описывает настройки HTTP-сервера редиректа.
type HTTPConfig struct {
	Addr string
}

// StoreConfig описывает настройки хранилища.
type StoreConfig struct {
	DataFile string
}

// TLSConfig описывает пути к сертификату и ключу для HTTPS.
type TLSConfig struct {
	CertFile string
	KeyFile  string
}

// Ошибки конфигурации. Возвращаются через errors.Is.
var (
	ErrMissingBotToken = errors.New("BOT_TOKEN не задан")
	ErrMissingBase     = errors.New("SHORT_URL_BASE не задан")
	ErrInvalidBase     = errors.New("SHORT_URL_BASE должен быть абсолютным http(s) URL")
	ErrIncompleteTLS   = errors.New("TLS_CERT_FILE и TLS_KEY_FILE задаются только вместе")
)

// NewConfig читает YAML-файл cfgName, переопределяет значения переменными окружения
// и валидирует обязательные поля. Приоритет: env > YAML > значение по умолчанию.
// Текст возвращаемых ошибок не содержит значения BOT_TOKEN.
func NewConfig(cfgName string) (*Config, error) {
	var cfg Config
	data, err := os.ReadFile(cfgName)
	if err != nil {
		return nil, fmt.Errorf("чтение файла конфигурации %q: %w", cfgName, err)
	}
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("разбор файла конфигурации %q: %w", cfgName, err)
	}

	cfg.Log.Level = firstNonEmpty(os.Getenv("LOG_LEVEL"), cfg.Log.Level, defaultLogLevel)
	cfg.Log.Format = firstNonEmpty(os.Getenv("LOG_FORMAT"), cfg.Log.Format, defaultLogFmt)
	if err := validateLog(cfg.Log); err != nil {
		return nil, err
	}

	cfg.Bot.Token = strings.TrimSpace(os.Getenv("BOT_TOKEN"))
	if cfg.Bot.Token == "" {
		return nil, ErrMissingBotToken
	}

	base, err := normalizeBase(os.Getenv("SHORT_URL_BASE"))
	if err != nil {
		return nil, err
	}
	cfg.Bot.ShortURLBase = base

	cfg.HTTP.Addr = firstNonEmpty(os.Getenv("HTTP_ADDR"), defaultHTTPAddr)
	cfg.Store.DataFile = firstNonEmpty(os.Getenv("DATA_FILE"), defaultDataFile)
	cfg.TLS.CertFile = strings.TrimSpace(os.Getenv("TLS_CERT_FILE"))
	cfg.TLS.KeyFile = strings.TrimSpace(os.Getenv("TLS_KEY_FILE"))
	if (cfg.TLS.CertFile == "") != (cfg.TLS.KeyFile == "") {
		return nil, ErrIncompleteTLS
	}

	return &cfg, nil
}

// normalizeBase проверяет, что SHORT_URL_BASE — абсолютный http(s) URL, и срезает хвостовой слэш.
func normalizeBase(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", ErrMissingBase
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", ErrInvalidBase
	}
	scheme := strings.ToLower(u.Scheme)
	if (scheme != "http" && scheme != "https") || u.Host == "" {
		return "", ErrInvalidBase
	}
	return strings.TrimRight(raw, "/"), nil
}

// validateLog проверяет допустимые уровни и форматы логирования.
func validateLog(cfg LogConfig) error {
	switch strings.ToLower(cfg.Level) {
	case "debug", "info", "warn", "error":
	default:
		return fmt.Errorf("LOG_LEVEL %q: допустимы debug, info, warn, error", cfg.Level)
	}
	switch strings.ToLower(cfg.Format) {
	case "text", "json":
	default:
		return fmt.Errorf("LOG_FORMAT %q: допустимы text, json", cfg.Format)
	}
	return nil
}

// firstNonEmpty возвращает первое непустое значение из переданных.
func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v = strings.TrimSpace(v); v != "" {
			return v
		}
	}
	return ""
}
