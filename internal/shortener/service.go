package shortener

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"
)

const (
	// maxCodeAttempts — число попыток подобрать свободный код до ErrCodeExhausted.
	maxCodeAttempts = 5
	// maxTitleFetches — предел одновременных фоновых загрузок заголовков.
	maxTitleFetches = 4
	// titleFetchTimeout — бюджет одной фоновой загрузки заголовка.
	titleFetchTimeout = 5 * time.Second
)

// TitleFetcher — часть, нужная сервису от загрузчика заголовков (объявлена
// потребителем). Реализация обязана уважать ctx и ограничивать работу по времени.
type TitleFetcher interface {
	Title(ctx context.Context, rawURL string) (string, error)
}

// Service сериализует идемпотентную запись; чтения идут напрямую в Store.
// Заголовки новых ссылок загружаются в фоне и пишутся отдельным UPDATE.
type Service struct {
	store   *Store
	titles  TitleFetcher
	logger  *slog.Logger
	writeMu sync.Mutex

	fetchMu   sync.Mutex
	fetchWG   sync.WaitGroup
	fetchSem  chan struct{}
	fetchDone chan struct{}
	closed    bool
}

// NewService создаёт сервис поверх хранилища. titles == nil — загрузка заголовков
// отключена (Title всегда ""); logger == nil — slog.Default().
func NewService(store *Store, titles TitleFetcher, logger *slog.Logger) *Service {
	if logger == nil {
		logger = slog.Default()
	}
	return &Service{
		store:     store,
		titles:    titles,
		logger:    logger,
		fetchSem:  make(chan struct{}, maxTitleFetches),
		fetchDone: make(chan struct{}),
	}
}

// Shorten возвращает существующую или создаёт новую ссылку для пары (userID, url).
// Возвращает ErrInvalidURL при невалидном вводе и ErrCodeExhausted при исчерпании попыток.
// Сетевое получение заголовка запускается после вставки, не блокируя ответ и не
// влияя на его результат.
func (s *Service) Shorten(ctx context.Context, userID int64, rawURL string) (Link, error) {
	url, err := ValidateURL(rawURL)
	if err != nil {
		return Link{}, err
	}

	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	existing, err := s.store.FindUserURL(ctx, userID, url)
	if err == nil {
		return existing, nil
	}
	if !errors.Is(err, ErrNotFound) {
		return Link{}, err
	}

	for attempt := 0; attempt < maxCodeAttempts; attempt++ {
		code, err := GenerateCode()
		if err != nil {
			return Link{}, err
		}
		exists, err := s.store.CodeExists(ctx, code)
		if err != nil {
			return Link{}, err
		}
		if exists {
			continue
		}
		link := Link{
			Code:      code,
			UserID:    userID,
			URL:       url,
			CreatedAt: time.Now().UTC(),
		}
		if err := s.store.Insert(ctx, link); err != nil {
			return Link{}, err
		}
		s.startTitleFetch(ctx, link.Code, url)
		return link, nil
	}
	return Link{}, ErrCodeExhausted
}

// Links возвращает ссылки пользователя в порядке создания.
func (s *Service) Links(ctx context.Context, userID int64) ([]Link, error) {
	return s.store.ByUser(ctx, userID)
}

// Resolve возвращает ссылку по коду или ErrNotFound.
func (s *Service) Resolve(ctx context.Context, code string) (Link, error) {
	return s.store.Resolve(ctx, code)
}

// Wait запрещает запуск новых фоновых загрузок, распускает ожидающих семафор и
// ждёт завершения активных. Возвращается не позже бюджета загрузки. Идемпотентен.
func (s *Service) Wait() {
	s.fetchMu.Lock()
	if !s.closed {
		s.closed = true
		close(s.fetchDone)
	}
	s.fetchMu.Unlock()
	s.fetchWG.Wait()
}

// startTitleFetch запускает фоновую загрузку заголовка, не блокируя вызывающего.
func (s *Service) startTitleFetch(parent context.Context, code, url string) {
	if s.titles == nil {
		return
	}

	s.fetchMu.Lock()
	if s.closed {
		s.fetchMu.Unlock()
		return
	}
	s.fetchWG.Add(1)
	s.fetchMu.Unlock()

	go func() {
		defer s.fetchWG.Done()

		select {
		case s.fetchSem <- struct{}{}:
			defer func() { <-s.fetchSem }()
		case <-s.fetchDone:
			return
		}

		ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), titleFetchTimeout)
		defer cancel()

		pageTitle, err := s.titles.Title(ctx, url)
		if err != nil {
			s.logger.Debug("не удалось получить заголовок ссылки", "url", url, "error", err)
			return
		}
		if pageTitle == "" {
			return
		}
		if err := s.store.SetTitleIfEmpty(ctx, code, TitleFor(pageTitle, url)); err != nil {
			s.logger.Error("сохранение заголовка ссылки", "code", code, "error", err)
		}
	}()
}
