package config

import (
	"log"
	"log/slog"
	"os"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

type Config struct {
	Log      LogConfig      `yaml:"log"`
}

type LogConfig struct {
	Level  string `yaml:"level"`
	Format string `yaml:"format"`
}

func NewConfig(cfgName string) *Config {
	var cfg Config
	file, err := os.Open(cfgName)
	if err != nil {
		log.Fatalf("open config file %s error: %v", cfgName, err)
	}
	defer func(file *os.File) {
		if err := file.Close(); err != nil {
			slog.Error(err.Error())
		}
	}(file)
	decoder := yaml.NewDecoder(file)
	if err = decoder.Decode(&cfg); err != nil {
		log.Fatalf("error decoding yaml: %v", err)
	}

	if cfg.Log.Level == "" {
		cfg.Log.Level = "info"
	}
	if cfg.Log.Format == "" {
		cfg.Log.Format = "text"
	}
	return &cfg
}
