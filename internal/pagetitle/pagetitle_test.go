package pagetitle

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

// roundTripFunc — транспорт-заглушка: тесты не обращаются к сети.
type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

// newTestFetcher создаёт Fetcher поверх транспорта-заглушки.
func newTestFetcher(fn roundTripFunc) *Fetcher {
	return New(&http.Client{Transport: fn})
}

// htmlResponse собирает ответ с заданными статусом, Content-Type и телом.
func htmlResponse(status int, contentType, body string) *http.Response {
	header := make(http.Header)
	if contentType != "" {
		header.Set("Content-Type", contentType)
	}
	return &http.Response{
		StatusCode: status,
		Header:     header,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

// redirectResponse собирает ответ-редирект с заданным Location.
func redirectResponse(location string) *http.Response {
	resp := htmlResponse(http.StatusFound, "", "")
	resp.Header.Set("Location", location)
	return resp
}

func TestTitleUnusable(t *testing.T) {
	lateCandidate := strings.Repeat("a", maxBodyBytes) + "<title>Поздний</title>"

	tests := []struct {
		name string
		resp *http.Response
	}{
		{name: "404", resp: htmlResponse(http.StatusNotFound, "text/html", `<title>Есть</title>`)},
		{name: "text/plain", resp: htmlResponse(http.StatusOK, "text/plain", `<title>Есть</title>`)},
		{name: "application/pdf", resp: htmlResponse(http.StatusOK, "application/pdf", `<title>Есть</title>`)},
		{name: "HTML без кандидатов", resp: htmlResponse(http.StatusOK, "text/html", `<html><body><h1>Только h1</h1></body></html>`)},
		{name: "кандидат за 512 KiB", resp: htmlResponse(http.StatusOK, "text/html", lateCandidate)},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fetch := newTestFetcher(func(*http.Request) (*http.Response, error) {
				return tt.resp, nil
			})
			got, err := fetch.Title(context.Background(), "https://example.com/")
			if err != nil {
				t.Fatalf("Title вернул ошибку: %v", err)
			}
			if got != "" {
				t.Errorf("Title = %q, ожидалась пустая строка", got)
			}
		})
	}

	t.Run("пустой Content-Type считается HTML", func(t *testing.T) {
		fetch := newTestFetcher(func(*http.Request) (*http.Response, error) {
			return htmlResponse(http.StatusOK, "", `<title>Заголовок</title>`), nil
		})
		got, err := fetch.Title(context.Background(), "https://example.com/")
		if err != nil {
			t.Fatalf("Title вернул ошибку: %v", err)
		}
		if got != "Заголовок" {
			t.Errorf("Title = %q, ожидалось %q", got, "Заголовок")
		}
	})
}

func TestTitleTimeout(t *testing.T) {
	fetch := newTestFetcher(func(req *http.Request) (*http.Response, error) {
		<-req.Context().Done()
		return nil, req.Context().Err()
	})

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	start := time.Now()
	got, err := fetch.Title(ctx, "https://example.com/")
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("Title с истёкшим ctx вернул nil-ошибку")
	}
	if got != "" {
		t.Errorf("Title = %q, ожидалась пустая строка", got)
	}
	if elapsed >= time.Second {
		t.Errorf("Title завершился за %v, ожидалось существенно меньше секунды", elapsed)
	}
}

func TestTitleRedirects(t *testing.T) {
	t.Run("шесть редиректов — ошибка", func(t *testing.T) {
		var requests int
		fetch := newTestFetcher(func(*http.Request) (*http.Response, error) {
			requests++
			return redirectResponse("/next"), nil
		})

		got, err := fetch.Title(context.Background(), "https://example.com/")
		if err == nil {
			t.Fatal("Title после шести редиректов вернул nil-ошибку")
		}
		if !errors.Is(err, errTooManyRedirects) {
			t.Errorf("ошибка = %v, ожидалась errTooManyRedirects", err)
		}
		if got != "" {
			t.Errorf("Title = %q, ожидалась пустая строка", got)
		}
		if want := maxRedirects + 1; requests != want {
			t.Errorf("запросов = %d, ожидалось %d", requests, want)
		}
	})

	t.Run("пять редиректов допустимы", func(t *testing.T) {
		var requests int
		fetch := newTestFetcher(func(*http.Request) (*http.Response, error) {
			requests++
			if requests <= maxRedirects {
				return redirectResponse("/next"), nil
			}
			return htmlResponse(http.StatusOK, "text/html", `<title>Цель</title>`), nil
		})

		got, err := fetch.Title(context.Background(), "https://example.com/")
		if err != nil {
			t.Fatalf("Title с пятью редиректами: %v", err)
		}
		if got != "Цель" {
			t.Errorf("Title = %q, ожидалось %q", got, "Цель")
		}
	})
}
