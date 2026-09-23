package shortener

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// newTestStoreAndService создаёт хранилище и сервис во временном каталоге.
// Cleanup сначала ждёт фоновые загрузки, затем закрывает БД (тот же порядок, что
// и в main).
func newTestStoreAndService(t *testing.T, titles TitleFetcher) (*Store, *Service) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "links.db")
	store, err := OpenStore(context.Background(), path)
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	svc := NewService(store, titles, nil)
	t.Cleanup(func() {
		svc.Wait()
		if err := store.Close(); err != nil {
			t.Errorf("Store.Close: %v", err)
		}
	})
	return store, svc
}

func newTestService(t *testing.T) *Service {
	t.Helper()
	_, svc := newTestStoreAndService(t, nil)
	return svc
}

// fakeFetcher — управляемый каналами TitleFetcher: вход в Title закрывает
// started, завершение ждёт release. Таймингов в тестах нет.
type fakeFetcher struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
	calls   atomic.Int64
	title   string
	err     error
}

func newFakeFetcher() *fakeFetcher {
	return &fakeFetcher{
		started: make(chan struct{}),
		release: make(chan struct{}),
	}
}

func (f *fakeFetcher) Title(ctx context.Context, _ string) (string, error) {
	f.calls.Add(1)
	f.once.Do(func() { close(f.started) })
	select {
	case <-f.release:
	case <-ctx.Done():
		return "", ctx.Err()
	}
	return f.title, f.err
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

func TestShortenAsyncTitle(t *testing.T) {
	ctx := context.Background()
	fetch := newFakeFetcher()
	fetch.title = "Заголовок страницы"
	store, svc := newTestStoreAndService(t, fetch)

	const url = "https://www.example.com/page"
	link, err := svc.Shorten(ctx, 1, url)
	if err != nil {
		t.Fatalf("Shorten: %v", err)
	}
	if link.Title != "" {
		t.Errorf("Shorten вернул Title = %q, ожидалась пустая строка до загрузки", link.Title)
	}

	stored, err := store.Resolve(ctx, link.Code)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if stored.Title != "" {
		t.Errorf("Title в БД = %q до завершения загрузки, ожидалась пустая строка", stored.Title)
	}

	<-fetch.started

	stored, err = store.Resolve(ctx, link.Code)
	if err != nil {
		t.Fatalf("Resolve во время загрузки: %v", err)
	}
	if stored.Title != "" {
		t.Errorf("Title в БД = %q во время загрузки, ожидалась пустая строка", stored.Title)
	}

	second, err := svc.Shorten(ctx, 1, url)
	if err != nil {
		t.Fatalf("повторный Shorten: %v", err)
	}
	if second.Code != link.Code {
		t.Errorf("повторный Shorten вернул код %q, ожидался %q", second.Code, link.Code)
	}
	if got := fetch.calls.Load(); got != 1 {
		t.Errorf("вызовов фетчера после повторного Shorten = %d, ожидался 1", got)
	}

	close(fetch.release)
	svc.Wait()

	stored, err = store.Resolve(ctx, link.Code)
	if err != nil {
		t.Fatalf("Resolve после Wait: %v", err)
	}
	if stored.Title != "Заголовок страницы" {
		t.Errorf("Title после Wait = %q, ожидался %q", stored.Title, "Заголовок страницы")
	}
	if got := stored.DisplayTitle(); got != "Заголовок страницы" {
		t.Errorf("DisplayTitle() = %q, ожидался заголовок страницы", got)
	}
}

func TestShortenAsyncIgnoresParentCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	fetch := newFakeFetcher()
	fetch.title = "Название"
	store, svc := newTestStoreAndService(t, fetch)

	link, err := svc.Shorten(ctx, 7, "https://example.com/x")
	if err != nil {
		t.Fatalf("Shorten: %v", err)
	}
	<-fetch.started
	cancel()
	close(fetch.release)
	svc.Wait()

	stored, err := store.Resolve(context.Background(), link.Code)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if stored.Title != "Название" {
		t.Errorf("Title после отмены родительского ctx = %q, ожидалось %q", stored.Title, "Название")
	}
}

func TestShortenAsyncFetchError(t *testing.T) {
	tests := []struct {
		name  string
		title string
		err   error
	}{
		{name: "ошибка фетчера", err: errors.New("сеть недоступна")},
		{name: "пустой заголовок"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ctx := context.Background()
			fetch := newFakeFetcher()
			fetch.title = tt.title
			fetch.err = tt.err
			close(fetch.release)
			store, svc := newTestStoreAndService(t, fetch)

			link, err := svc.Shorten(ctx, 1, "https://www.example.com/page")
			if err != nil {
				t.Fatalf("Shorten: %v", err)
			}
			<-fetch.started
			svc.Wait()

			if got := fetch.calls.Load(); got != 1 {
				t.Errorf("вызовов фетчера = %d, ожидался 1", got)
			}
			stored, err := store.Resolve(ctx, link.Code)
			if err != nil {
				t.Fatalf("Resolve: %v", err)
			}
			if stored.Title != "" {
				t.Errorf("Title в БД = %q, ожидалась пустая строка", stored.Title)
			}
			if got := stored.DisplayTitle(); got != "example.com" {
				t.Errorf("DisplayTitle() = %q, ожидалось %q", got, "example.com")
			}
		})
	}
}

func TestSetTitleIfEmpty(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	link := Link{Code: "ttl0001", UserID: 1, URL: "https://example.com/x", CreatedAt: time.Now().UTC()}
	if err := store.Insert(ctx, link); err != nil {
		t.Fatalf("Insert: %v", err)
	}
	if err := store.SetTitleIfEmpty(ctx, link.Code, "Первый"); err != nil {
		t.Fatalf("SetTitleIfEmpty первый: %v", err)
	}
	if err := store.SetTitleIfEmpty(ctx, link.Code, "Второй"); err != nil {
		t.Fatalf("SetTitleIfEmpty второй: %v", err)
	}

	got, err := store.Resolve(ctx, link.Code)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if got.Title != "Первый" {
		t.Errorf("Title = %q, ожидалось %q (перезапись запрещена)", got.Title, "Первый")
	}

	if err := store.SetTitleIfEmpty(ctx, "missing", "X"); err != nil {
		t.Errorf("SetTitleIfEmpty отсутствующего кода: %v, ожидался nil (0 строк — норма)", err)
	}
}

func TestShortenWaitRejectsNewFetches(t *testing.T) {
	ctx := context.Background()
	fetch := newFakeFetcher()
	close(fetch.release)
	store, svc := newTestStoreAndService(t, fetch)

	svc.Wait()
	svc.Wait() // идемпотентность

	link, err := svc.Shorten(ctx, 1, "https://example.com/x")
	if err != nil {
		t.Fatalf("Shorten после Wait: %v", err)
	}
	if got := fetch.calls.Load(); got != 0 {
		t.Errorf("вызовов фетчера после Wait = %d, ожидался 0", got)
	}
	stored, err := store.Resolve(ctx, link.Code)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if stored.Title != "" {
		t.Errorf("Title в БД = %q, ожидалась пустая строка", stored.Title)
	}
}
