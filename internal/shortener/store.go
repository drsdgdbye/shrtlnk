package shortener

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite" // регистрирует SQL-драйвер "sqlite" (чистый Go, CGO_ENABLED=0)
)

// schemaVersion — текущая версия схемы БД.
const schemaVersion = 1

// Store — адаптер SQLite. Потокобезопасен за счёт пула database/sql.
type Store struct {
	db *sql.DB
}

// OpenStore открывает (при необходимости создаёт каталог) БД по пути path,
// применяет pragmas и миграцию схемы.
func OpenStore(ctx context.Context, path string) (*Store, error) {
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("создание каталога %q для БД: %w", dir, err)
		}
	}

	dsn := "file:" + path +
		"?_pragma=busy_timeout(5000)" +
		"&_pragma=journal_mode(WAL)" +
		"&_pragma=synchronous(NORMAL)" +
		"&_pragma=foreign_keys(1)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("открытие БД %q: %w", path, err)
	}
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(4)

	if err := db.PingContext(ctx); err != nil {
		if cerr := db.Close(); cerr != nil {
			return nil, fmt.Errorf("проверка соединения с БД %q: %w; закрытие: %v", path, err, cerr)
		}
		return nil, fmt.Errorf("проверка соединения с БД %q: %w", path, err)
	}

	store := &Store{db: db}
	if err := store.migrate(ctx); err != nil {
		if cerr := db.Close(); cerr != nil {
			return nil, fmt.Errorf("%w; закрытие БД: %v", err, cerr)
		}
		return nil, err
	}
	return store, nil
}

// migrate создаёт схему при user_version = 0 и отказывается работать с БД новее бинарника.
func (s *Store) migrate(ctx context.Context) error {
	var version int
	if err := s.db.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version); err != nil {
		return fmt.Errorf("чтение версии схемы БД: %w", err)
	}
	if version > schemaVersion {
		return fmt.Errorf("версия схемы БД %d новее поддерживаемой %d", version, schemaVersion)
	}
	if version == schemaVersion {
		return nil
	}

	const createTable = `CREATE TABLE IF NOT EXISTS links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL,
    user_id    INTEGER NOT NULL,
    url        TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (code),
    UNIQUE (user_id, url)
);`
	const createIndex = `CREATE INDEX IF NOT EXISTS idx_links_user_id_id ON links (user_id, id);`
	const setVersion = `PRAGMA user_version = 1;`

	for _, stmt := range []string{createTable, createIndex, setVersion} {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("миграция схемы БД: %w", err)
		}
	}
	return nil
}

// Close закрывает соединение с БД.
func (s *Store) Close() error {
	if err := s.db.Close(); err != nil {
		return fmt.Errorf("закрытие БД: %w", err)
	}
	return nil
}

// FindUserURL возвращает ссылку пользователя userID с точным URL url или ErrNotFound.
func (s *Store) FindUserURL(ctx context.Context, userID int64, url string) (Link, error) {
	const query = `SELECT code, user_id, url, created_at FROM links WHERE user_id = ? AND url = ?`
	return s.scanLink(ctx, query, userID, url)
}

// Resolve возвращает ссылку по коду или ErrNotFound.
func (s *Store) Resolve(ctx context.Context, code string) (Link, error) {
	const query = `SELECT code, user_id, url, created_at FROM links WHERE code = ?`
	return s.scanLink(ctx, query, code)
}

// CodeExists сообщает, занят ли уже короткий код code.
func (s *Store) CodeExists(ctx context.Context, code string) (bool, error) {
	const query = `SELECT 1 FROM links WHERE code = ? LIMIT 1`
	var one int
	err := s.db.QueryRowContext(ctx, query, code).Scan(&one)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("проверка кода %q: %w", code, err)
	}
	return true, nil
}

// ByUser возвращает ссылки пользователя в порядке создания (ORDER BY id ASC).
func (s *Store) ByUser(ctx context.Context, userID int64) ([]Link, error) {
	const query = `SELECT code, user_id, url, created_at FROM links WHERE user_id = ? ORDER BY id ASC`
	rows, err := s.db.QueryContext(ctx, query, userID)
	if err != nil {
		return nil, fmt.Errorf("выборка ссылок пользователя: %w", err)
	}
	defer rows.Close()

	links := make([]Link, 0)
	for rows.Next() {
		link, err := scanRow(rows.Scan)
		if err != nil {
			return nil, err
		}
		links = append(links, link)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("чтение ссылок пользователя: %w", err)
	}
	return links, nil
}

// Insert сохраняет новую ссылку. Нарушение UNIQUE возвращается ошибкой.
func (s *Store) Insert(ctx context.Context, link Link) error {
	const query = `INSERT INTO links (code, user_id, url, created_at) VALUES (?, ?, ?, ?)`
	_, err := s.db.ExecContext(ctx, query,
		link.Code,
		link.UserID,
		link.URL,
		link.CreatedAt.UTC().Format(time.RFC3339Nano),
	)
	if err != nil {
		return fmt.Errorf("вставка ссылки: %w", err)
	}
	return nil
}

// scanLink выполняет запрос, ожидающий ровно одну строку, и разбирает её в Link.
func (s *Store) scanLink(ctx context.Context, query string, args ...any) (Link, error) {
	row := s.db.QueryRowContext(ctx, query, args...)
	link, err := scanRow(row.Scan)
	if errors.Is(err, sql.ErrNoRows) {
		return Link{}, ErrNotFound
	}
	if err != nil {
		return Link{}, err
	}
	return link, nil
}

// scanRow разбирает одну строку выборки в Link.
func scanRow(scan func(dest ...any) error) (Link, error) {
	var (
		link      Link
		createdAt string
	)
	if err := scan(&link.Code, &link.UserID, &link.URL, &createdAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Link{}, err
		}
		return Link{}, fmt.Errorf("разбор строки ссылки: %w", err)
	}
	parsed, err := time.Parse(time.RFC3339Nano, createdAt)
	if err != nil {
		return Link{}, fmt.Errorf("разбор времени создания %q: %w", createdAt, err)
	}
	link.CreatedAt = parsed.UTC()
	return link, nil
}
