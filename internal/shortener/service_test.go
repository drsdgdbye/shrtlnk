package shortener

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
)

func newTestService(t *testing.T) *Service {
	t.Helper()
	path := filepath.Join(t.TempDir(), "links.db")
	store, err := OpenStore(context.Background(), path)
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	t.Cleanup(func() {
		if err := store.Close(); err != nil {
			t.Errorf("Store.Close: %v", err)
		}
	})
	return NewService(store)
}

func TestShortenIdempotent(t *testing.T) {
	ctx := context.Background()
	svc := newTestService(t)

	const url = "https://example.com/page?x=1&y=2"
	first, err := svc.Shorten(ctx, 1, url)
	if err != nil {
		t.Fatalf("Shorten первый раз: %v", err)
	}
	second, err := svc.Shorten(ctx, 1, url)
	if err != nil {
		t.Fatalf("Shorten повторно: %v", err)
	}
	if second.Code != first.Code {
		t.Errorf("повторный Shorten дал код %q, ожидался %q", second.Code, first.Code)
	}
	if second.URL != url {
		t.Errorf("сохранённый URL = %q, ожидался %q", second.URL, url)
	}

	other, err := svc.Shorten(ctx, 2, url)
	if err != nil {
		t.Fatalf("Shorten другого пользователя: %v", err)
	}
	if other.Code == first.Code {
		t.Errorf("разные пользователи получили одинаковый код %q", other.Code)
	}

	const workers = 16
	const concurrentURL = "https://example.com/concurrent"
	codes := make(chan string, workers)
	errs := make(chan error, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			link, err := svc.Shorten(ctx, 3, concurrentURL)
			if err != nil {
				errs <- err
				return
			}
			codes <- link.Code
		}()
	}
	wg.Wait()
	close(codes)
	close(errs)

	for err := range errs {
		t.Errorf("конкурентный Shorten: %v", err)
	}
	var got string
	distinct := make(map[string]struct{})
	for code := range codes {
		distinct[code] = struct{}{}
		got = code
	}
	if len(distinct) != 1 {
		t.Errorf("конкурентный Shorten вернул %d различных кодов: %v", len(distinct), distinct)
	}
	if got == "" {
		t.Fatal("конкурентный Shorten не вернул ни одного кода")
	}

	links, err := svc.Links(ctx, 3)
	if err != nil {
		t.Fatalf("Links: %v", err)
	}
	if len(links) != 1 {
		t.Errorf("после конкурентного Shorten сохранено %d записей, ожидалась 1", len(links))
	}
}

func TestLinks(t *testing.T) {
	ctx := context.Background()
	svc := newTestService(t)

	urls := []string{"https://a.example/1", "https://a.example/2", "https://a.example/3"}
	want := make([]string, 0, len(urls))
	for _, u := range urls {
		link, err := svc.Shorten(ctx, 1, u)
		if err != nil {
			t.Fatalf("Shorten(%q): %v", u, err)
		}
		want = append(want, link.Code)
	}
	if _, err := svc.Shorten(ctx, 2, "https://b.example/1"); err != nil {
		t.Fatalf("Shorten второго пользователя: %v", err)
	}

	got, err := svc.Links(ctx, 1)
	if err != nil {
		t.Fatalf("Links(1): %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("Links(1) вернул %d ссылок, ожидалось %d", len(got), len(want))
	}
	for i, link := range got {
		if link.Code != want[i] {
			t.Errorf("Links(1)[%d].Code = %q, ожидался %q (порядок создания)", i, link.Code, want[i])
		}
		if link.UserID != 1 {
			t.Errorf("Links(1)[%d].UserID = %d, ожидался 1", i, link.UserID)
		}
	}

	other, err := svc.Links(ctx, 2)
	if err != nil {
		t.Fatalf("Links(2): %v", err)
	}
	if len(other) != 1 || other[0].UserID != 2 {
		t.Errorf("Links(2) = %+v, ожидалась одна ссылка пользователя 2", other)
	}
}

func TestShortenInvalidURL(t *testing.T) {
	ctx := context.Background()
	svc := newTestService(t)

	if _, err := svc.Shorten(ctx, 1, "not a link"); !errors.Is(err, ErrInvalidURL) {
		t.Errorf("Shorten(невалидный) = %v, ожидалась ErrInvalidURL", err)
	}
}
