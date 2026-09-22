// Package pagetitle получает заголовок HTML-страницы по ссылке.
package pagetitle

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Timeout — бюджет одной загрузки заголовка (запрос + чтение + разбор).
const Timeout = 5 * time.Second

const (
	// maxBodyBytes — предел сырого тела ответа, участвующего в разборе (512 KiB).
	maxBodyBytes = 512 << 10
	// maxRedirects — предельное число переходов по редиректам.
	maxRedirects = 5
	// userAgent — значение заголовка User-Agent запроса.
	userAgent = "shrtlnk-bot/1.0"
	// acceptHeader — значение заголовка Accept запроса.
	acceptHeader = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
)

// errTooManyRedirects — превышен предел переходов по редиректам.
var errTooManyRedirects = errors.New("превышено число редиректов")

// Fetcher загружает страницу и возвращает её заголовок. Потокобезопасен.
type Fetcher struct {
	client *http.Client
}

// New создаёт Fetcher поверх client. client == nil — безопасный клиент по
// умолчанию: транспорт с блокировкой непубличных адресов (newTransport).
// Переданный клиент не изменяется: Fetcher работает с его копией.
func New(client *http.Client) *Fetcher {
	if client == nil {
		client = &http.Client{Transport: newTransport()}
	}
	cloned := *client
	cloned.CheckRedirect = func(_ *http.Request, via []*http.Request) error {
		if len(via) > maxRedirects {
			return errTooManyRedirects
		}
		return nil
	}
	return &Fetcher{client: &cloned}
}

// Title возвращает первый непустой заголовок страницы rawURL: <title> или
// og:title — что выше по документу. ("", nil) — если заголовка нет или ответ
// непригоден (не 2xx, не HTML, тело не прочитано). error — сбой
// транспорта/бюджета времени (в том числе блокировка адреса).
func (f *Fetcher) Title(ctx context.Context, rawURL string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, Timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", fmt.Errorf("создание запроса %q: %w", rawURL, err)
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", acceptHeader)

	resp, err := f.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("загрузка страницы %q: %w", rawURL, err)
	}
	defer func() {
		// Тело всё равно прочитано не до конца (кандидат мог не найтись),
		// поэтому закрытие без return-ошибки: отдаём соединение пулу.
		_ = resp.Body.Close()
	}()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", nil
	}
	if !htmlContentType(resp.Header.Get("Content-Type")) {
		return "", nil
	}

	return extractTitle(io.LimitReader(resp.Body, maxBodyBytes), resp.Header.Get("Content-Type")), nil
}

// htmlContentType сообщает, допустим ли Content-Type для разбора как HTML:
// text/html или application/xhtml+xml (параметры отрезаны, регистр не важен,
// пустое/отсутствующее значение считается HTML).
func htmlContentType(contentType string) bool {
	if contentType == "" {
		return true
	}
	mediaType, _, _ := strings.Cut(contentType, ";")
	switch strings.ToLower(strings.TrimSpace(mediaType)) {
	case "text/html", "application/xhtml+xml":
		return true
	default:
		return false
	}
}
