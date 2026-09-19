package shortener

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

// newTestStore создаёт хранилище во временном каталоге и закрывает его по завершении теста.
func newTestStore(t *testing.T) *Store {
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
	return store
}

func TestStorePersist(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "links.db")

	store, err := OpenStore(ctx, path)
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	want := Link{Code: "abc1234", UserID: 7, URL: "https://example.com/x?y=1", CreatedAt: time.Now().UTC()}
	if err := store.Insert(ctx, want); err != nil {
		t.Fatalf("Insert: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	reopened, err := OpenStore(ctx, path)
	if err != nil {
		t.Fatalf("повторный OpenStore: %v", err)
	}
	t.Cleanup(func() {
		if err := reopened.Close(); err != nil {
			t.Errorf("reopened.Close: %v", err)
		}
	})

	got, err := reopened.Resolve(ctx, want.Code)
	if err != nil {
		t.Fatalf("Resolve после переоткрытия: %v", err)
	}
	if got.Code != want.Code || got.UserID != want.UserID || got.URL != want.URL {
		t.Errorf("Resolve = %+v, ожидалось %+v", got, want)
	}
	if !got.CreatedAt.Equal(want.CreatedAt) {
		t.Errorf("CreatedAt = %v, ожидалось %v", got.CreatedAt, want.CreatedAt)
	}

	links, err := reopened.ByUser(ctx, want.UserID)
	if err != nil {
		t.Fatalf("ByUser после переоткрытия: %v", err)
	}
	if len(links) != 1 || links[0].Code != want.Code {
		t.Errorf("ByUser = %+v, ожидалась одна запись %q", links, want.Code)
	}
}

func TestStoreConstraints(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	first := Link{Code: "code001", UserID: 1, URL: "https://example.com/a", CreatedAt: time.Now().UTC()}
	if err := store.Insert(ctx, first); err != nil {
		t.Fatalf("Insert первой ссылки: %v", err)
	}
	if err := store.Insert(ctx, Link{Code: "code002", UserID: 1, URL: first.URL, CreatedAt: time.Now().UTC()}); err == nil {
		t.Error("ожидалась ошибка UNIQUE(user_id, url) при дубле пары")
	}
	if err := store.Insert(ctx, Link{Code: first.Code, UserID: 2, URL: "https://example.com/b", CreatedAt: time.Now().UTC()}); err == nil {
		t.Error("ожидалась ошибка UNIQUE(code) при дубле кода")
	}

	ordered := []string{"ord0001", "ord0002", "ord0003"}
	for _, code := range ordered {
		link := Link{Code: code, UserID: 1, URL: "https://example.com/" + code, CreatedAt: time.Now().UTC()}
		if err := store.Insert(ctx, link); err != nil {
			t.Fatalf("Insert %q: %v", code, err)
		}
	}

	links, err := store.ByUser(ctx, 1)
	if err != nil {
		t.Fatalf("ByUser: %v", err)
	}
	want := append([]string{first.Code}, ordered...)
	if len(links) != len(want) {
		t.Fatalf("ByUser вернул %d ссылок, ожидалось %d", len(links), len(want))
	}
	for i, link := range links {
		if link.Code != want[i] {
			t.Errorf("ByUser[%d].Code = %q, ожидалось %q (порядок по id)", i, link.Code, want[i])
		}
	}
}

func TestStoreNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	if _, err := store.Resolve(ctx, "missing"); !errors.Is(err, ErrNotFound) {
		t.Errorf("Resolve неизвестного кода: %v, ожидалась ErrNotFound", err)
	}
	if _, err := store.FindUserURL(ctx, 42, "https://example.com/x"); !errors.Is(err, ErrNotFound) {
		t.Errorf("FindUserURL без записи: %v, ожидалась ErrNotFound", err)
	}
	exists, err := store.CodeExists(ctx, "missing")
	if err != nil {
		t.Fatalf("CodeExists: %v", err)
	}
	if exists {
		t.Error("CodeExists вернул true для отсутствующего кода")
	}
}
