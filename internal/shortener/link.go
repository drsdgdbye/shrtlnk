// Package shortener отвечает за валидацию ссылок, генерацию коротких кодов
// и персистентное хранение соответствий «код → URL».
package shortener

import (
	"crypto/rand"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"
)

// Параметры коротких кодов и ограничения входных ссылок.
const (
	// CodeLength — длина короткого кода в символах.
	CodeLength = 7
	// MaxURLLength — максимальная длина принимаемой ссылки после TrimSpace.
	MaxURLLength = 2048
	// codeAlphabet — base62-алфавит короткого кода.
	codeAlphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
	// rejectionLimit — граница rejection sampling: 256 - (256 % 62) = 248.
	rejectionLimit = 248
)

// Ошибки пакета. Возвращаются через errors.Is.
var (
	// ErrInvalidURL — входная строка не является абсолютным http(s) URL.
	ErrInvalidURL = errors.New("некорректная ссылка")
	// ErrNotFound — ссылка с запрошенным кодом отсутствует.
	ErrNotFound = errors.New("ссылка не найдена")
	// ErrCodeExhausted — не удалось подобрать свободный код за отведённые попытки.
	ErrCodeExhausted = errors.New("не удалось сгенерировать уникальный код")
)

// Link описывает сохранённую короткую ссылку.
type Link struct {
	Code      string    `json:"code"`
	UserID    int64     `json:"user_id"`
	URL       string    `json:"url"`
	CreatedAt time.Time `json:"created_at"`
}

// ValidateURL обрезает пробелы, проверяет длину (1..MaxURLLength), парсит url.Parse,
// требует схему http/https (без учёта регистра) и непустой Host.
// Возвращает обрезанную строку как канонический URL.
func ValidateURL(raw string) (string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" || len(trimmed) > MaxURLLength {
		return "", ErrInvalidURL
	}
	u, err := url.Parse(trimmed)
	if err != nil {
		return "", ErrInvalidURL
	}
	scheme := strings.ToLower(u.Scheme)
	if (scheme != "http" && scheme != "https") || u.Host == "" {
		return "", ErrInvalidURL
	}
	return trimmed, nil
}

// GenerateCode возвращает криптографически случайный код длины CodeLength из base62.
// Используется rejection sampling: байты >= 248 отбрасываются, чтобы модульное
// смещение не искажало распределение.
func GenerateCode() (string, error) {
	code := make([]byte, CodeLength)
	buf := make([]byte, CodeLength)
	for i := 0; i < CodeLength; {
		if _, err := rand.Read(buf); err != nil {
			return "", fmt.Errorf("генерация короткого кода: %w", err)
		}
		for _, b := range buf {
			if b >= rejectionLimit {
				continue
			}
			code[i] = codeAlphabet[int(b)%len(codeAlphabet)]
			i++
			if i == CodeLength {
				break
			}
		}
	}
	return string(code), nil
}
