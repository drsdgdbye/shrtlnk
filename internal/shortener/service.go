package shortener

import (
	"context"
	"errors"
	"sync"
	"time"
)

// maxCodeAttempts — число попыток подобрать свободный код до ErrCodeExhausted.
const maxCodeAttempts = 5

// Service сериализует идемпотентную запись; чтения идут напрямую в Store.
type Service struct {
	store   *Store
	writeMu sync.Mutex
}

// NewService создаёт сервис поверх хранилища.
func NewService(store *Store) *Service {
	return &Service{store: store}
}

// Shorten возвращает существующую или создаёт новую ссылку для пары (userID, url).
// Возвращает ErrInvalidURL при невалидном вводе и ErrCodeExhausted при исчерпании попыток.
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
