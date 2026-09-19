package redirect

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"yt_dw/internal/shortener"
)

type fakeResolver struct {
	links map[string]string
	err   error
}

func (f fakeResolver) Resolve(_ context.Context, code string) (shortener.Link, error) {
	if f.err != nil {
		return shortener.Link{}, f.err
	}
	url, ok := f.links[code]
	if !ok {
		return shortener.Link{}, shortener.ErrNotFound
	}
	return shortener.Link{Code: code, URL: url}, nil
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestHandler(t *testing.T) {
	const target = "https://example.com/target?x=1"
	handler := NewHandler(fakeResolver{links: map[string]string{"abc1234": target}}, testLogger())

	tests := []struct {
		name       string
		method     string
		path       string
		wantStatus int
		wantBody   string
		wantLoc    string
	}{
		{name: "известный код", method: http.MethodGet, path: "/abc1234", wantStatus: http.StatusFound, wantLoc: target},
		{name: "неизвестный код", method: http.MethodGet, path: "/nope", wantStatus: http.StatusNotFound, wantBody: "not found"},
		{name: "healthz", method: http.MethodGet, path: "/healthz", wantStatus: http.StatusOK, wantBody: "ok"},
		{name: "корень", method: http.MethodGet, path: "/", wantStatus: http.StatusNotFound},
		{name: "неверный метод", method: http.MethodPost, path: "/abc1234", wantStatus: http.StatusMethodNotAllowed},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(tt.method, tt.path, nil)
			handler.ServeHTTP(rec, req)

			if rec.Code != tt.wantStatus {
				t.Errorf("статус = %d, ожидался %d", rec.Code, tt.wantStatus)
			}
			if tt.wantLoc != "" && rec.Header().Get("Location") != tt.wantLoc {
				t.Errorf("Location = %q, ожидался %q", rec.Header().Get("Location"), tt.wantLoc)
			}
			if tt.wantBody != "" && rec.Body.String() != tt.wantBody {
				t.Errorf("тело = %q, ожидалось %q", rec.Body.String(), tt.wantBody)
			}
		})
	}
}

func TestHandlerQuery(t *testing.T) {
	const target = "https://example.com/p?a=1&b=2"
	handler := NewHandler(fakeResolver{links: map[string]string{"q1": target}}, testLogger())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/q1", nil)
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusFound {
		t.Fatalf("статус = %d, ожидался 302", rec.Code)
	}
	if got := rec.Header().Get("Location"); got != target {
		t.Errorf("Location = %q, ожидалось байт-в-байт %q", got, target)
	}
}
