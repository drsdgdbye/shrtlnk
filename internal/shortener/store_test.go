package shortener

import (
	"context"
	"database/sql"
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
	want := Link{Code: "abc1234", UserID: 7, URL: "https://example.com/x?y=1", Title: "Заголовок страницы", CreatedAt: time.Now().UTC()}
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
	if got.Code != want.Code || got.UserID != want.UserID || got.URL != want.URL || got.Title != want.Title {
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

// createV1DB создаёт БД со старой схемой v1 (без колонки title) и одной строкой.
func createV1DB(t *testing.T, path string) {
	t.Helper()
	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		t.Fatalf("sql.Open v1: %v", err)
	}
	const ddl = `CREATE TABLE links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    url        TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (code),
    UNIQUE (user_id, url)
);`
	const insert = `INSERT INTO links (code, user_id, url, created_at)
VALUES ('old0001', 1, 'https://example.com/old', '2026-01-01T00:00:00Z');`
	for _, stmt := range []string{ddl, insert, `PRAGMA user_version = 1;`} {
		if _, err := db.Exec(stmt); err != nil {
			if cerr := db.Close(); cerr != nil {
				t.Fatalf("подготовка БД v1 (%v); Close: %v", err, cerr)
			}
			t.Fatalf("подготовка БД v1: %v", err)
		}
	}
	if err := db.Close(); err != nil {
		t.Fatalf("Close БД v1: %v", err)
	}
}

// storeVersion читает PRAGMA user_version напрямую через соединение хранилища.
func storeVersion(t *testing.T, store *Store) int {
	t.Helper()
	var version int
	if err := store.db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		t.Fatalf("чтение user_version: %v", err)
	}
	return version
}

func TestStoreMigrateTitle(t *testing.T) {
	ctx := context.Background()

	t.Run("v1 добавляет колонку", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "v1.db")
		createV1DB(t, path)

		store, err := OpenStore(ctx, path)
		if err != nil {
			t.Fatalf("OpenStore поверх v1: %v", err)
		}
		t.Cleanup(func() {
			if err := store.Close(); err != nil {
				t.Errorf("store.Close: %v", err)
			}
		})

		if got := storeVersion(t, store); got != schemaVersion {
			t.Errorf("user_version = %d, ожидалось %d", got, schemaVersion)
		}
		got, err := store.Resolve(ctx, "old0001")
		if err != nil {
			t.Fatalf("Resolve старой строки: %v", err)
		}
		if got.Title != "" {
			t.Errorf("Title старой строки = %q, ожидалась пустая строка", got.Title)
		}
		if err := store.SetTitleIfEmpty(ctx, got.Code, "Обновлено"); err != nil {
			t.Fatalf("SetTitleIfEmpty после миграции: %v", err)
		}
		if got, err = store.Resolve(ctx, "old0001"); err != nil {
			t.Fatalf("Resolve после записи: %v", err)
		}
		if got.Title != "Обновлено" {
			t.Errorf("Title после записи = %q, ожидалось %q", got.Title, "Обновлено")
		}
	})

	t.Run("версия новее бинарника — ошибка", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "v3.db")
		db, err := sql.Open("sqlite", "file:"+path)
		if err != nil {
			t.Fatalf("sql.Open: %v", err)
		}
		if _, err := db.Exec(`PRAGMA user_version = 3;`); err != nil {
			if cerr := db.Close(); cerr != nil {
				t.Fatalf("установка user_version (%v); Close: %v", err, cerr)
			}
			t.Fatalf("установка user_version: %v", err)
		}
		if err := db.Close(); err != nil {
			t.Fatalf("Close: %v", err)
		}

		if _, err := OpenStore(ctx, path); err == nil {
			t.Error("OpenStore БД версии 3 вернул nil, ожидалась ошибка запуска")
		}
	})
}

func TestStoreTitleRoundTrip(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	want := Link{Code: "ttl0001", UserID: 3, URL: "https://example.com/x", Title: "Название страницы", CreatedAt: time.Now().UTC()}
	if err := store.Insert(ctx, want); err != nil {
		t.Fatalf("Insert: %v", err)
	}

	byCode, err := store.Resolve(ctx, want.Code)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if byCode.Title != want.Title {
		t.Errorf("Resolve.Title = %q, ожидалось %q", byCode.Title, want.Title)
	}

	byURL, err := store.FindUserURL(ctx, want.UserID, want.URL)
	if err != nil {
		t.Fatalf("FindUserURL: %v", err)
	}
	if byURL.Title != want.Title {
		t.Errorf("FindUserURL.Title = %q, ожидалось %q", byURL.Title, want.Title)
	}

	links, err := store.ByUser(ctx, want.UserID)
	if err != nil {
		t.Fatalf("ByUser: %v", err)
	}
	if len(links) != 1 || links[0].Title != want.Title {
		t.Errorf("ByUser = %+v, ожидалась одна запись с Title %q", links, want.Title)
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
