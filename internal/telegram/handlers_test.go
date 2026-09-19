package telegram

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"

	tgbot "github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"

	"yt_dw/internal/shortener"
)

const testBase = "https://shrt.example"

type sentMessage struct {
	chatID    any
	text      string
	parseMode models.ParseMode
	disabled  bool
}

type fakeSender struct {
	mu       sync.Mutex
	messages []sentMessage
}

func (f *fakeSender) SendMessage(_ context.Context, params *tgbot.SendMessageParams) (*models.Message, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.messages = append(f.messages, sentMessage{
		chatID:    params.ChatID,
		text:      params.Text,
		parseMode: params.ParseMode,
		disabled: params.LinkPreviewOptions != nil &&
			params.LinkPreviewOptions.IsDisabled != nil &&
			*params.LinkPreviewOptions.IsDisabled,
	})
	return &models.Message{}, nil
}

func (f *fakeSender) sent() []sentMessage {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]sentMessage, len(f.messages))
	copy(out, f.messages)
	return out
}

type fakeService struct {
	shorten func(ctx context.Context, userID int64, rawURL string) (shortener.Link, error)
	links   func(ctx context.Context, userID int64) ([]shortener.Link, error)
}

func (f fakeService) Shorten(ctx context.Context, userID int64, rawURL string) (shortener.Link, error) {
	if f.shorten == nil {
		return shortener.Link{}, errors.New("Shorten не ожидался")
	}
	return f.shorten(ctx, userID, rawURL)
}

func (f fakeService) Links(ctx context.Context, userID int64) ([]shortener.Link, error) {
	if f.links == nil {
		return nil, errors.New("Links не ожидался")
	}
	return f.links(ctx, userID)
}

func newTestHandlers(svc shortenerService, sender messageSender) *handlers {
	return newHandlers(svc, testBase, slog.New(slog.NewTextHandler(io.Discard, nil)), sender)
}

func privateUpdate(userID int64, text string) *models.Update {
	return &models.Update{Message: &models.Message{
		Chat: models.Chat{ID: userID, Type: models.ChatTypePrivate},
		From: &models.User{ID: userID},
		Text: text,
	}}
}

func TestDefaultNonText(t *testing.T) {
	sender := &fakeSender{}
	svc := fakeService{shorten: func(context.Context, int64, string) (shortener.Link, error) {
		t.Error("Shorten не должен вызываться для не-текстового сообщения")
		return shortener.Link{}, nil
	}}
	h := newTestHandlers(svc, sender)

	update := privateUpdate(1, "")
	update.Message.Photo = []models.PhotoSize{{FileID: "photo"}}
	h.handleDefault(context.Background(), update)

	got := sender.sent()
	if len(got) != 1 {
		t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
	}
	if got[0].text != fallbackText {
		t.Errorf("текст = %q, ожидался %q", got[0].text, fallbackText)
	}
}

func TestDefaultInvalidText(t *testing.T) {
	sender := &fakeSender{}
	svc := fakeService{shorten: func(context.Context, int64, string) (shortener.Link, error) {
		return shortener.Link{}, shortener.ErrInvalidURL
	}}
	h := newTestHandlers(svc, sender)

	h.handleDefault(context.Background(), privateUpdate(1, "not a link"))

	got := sender.sent()
	if len(got) != 1 {
		t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
	}
	if got[0].text != fallbackText {
		t.Errorf("текст = %q, ожидался %q", got[0].text, fallbackText)
	}
}

func TestDefaultValidLink(t *testing.T) {
	sender := &fakeSender{}
	svc := fakeService{shorten: func(_ context.Context, userID int64, rawURL string) (shortener.Link, error) {
		if userID != 42 {
			t.Errorf("userID = %d, ожидался 42", userID)
		}
		if rawURL != "https://example.com/very/long?x=1" {
			t.Errorf("rawURL = %q", rawURL)
		}
		return shortener.Link{Code: "abc1234", UserID: userID, URL: rawURL}, nil
	}}
	h := newTestHandlers(svc, sender)

	h.handleDefault(context.Background(), privateUpdate(42, "https://example.com/very/long?x=1"))

	got := sender.sent()
	if len(got) != 1 {
		t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
	}
	want := "Your short link: <a href=\"https://shrt.example/abc1234\">https://shrt.example/abc1234</a>"
	if got[0].text != want {
		t.Errorf("текст = %q, ожидался %q", got[0].text, want)
	}
	if got[0].parseMode != models.ParseModeHTML {
		t.Errorf("ParseMode = %q, ожидался %q", got[0].parseMode, models.ParseModeHTML)
	}
	if !got[0].disabled {
		t.Error("превью ссылки должно быть отключено")
	}
}

func TestLinksCommand(t *testing.T) {
	t.Run("есть ссылки", func(t *testing.T) {
		sender := &fakeSender{}
		svc := fakeService{links: func(_ context.Context, userID int64) ([]shortener.Link, error) {
			if userID != 7 {
				t.Errorf("userID = %d, ожидался 7", userID)
			}
			return []shortener.Link{{Code: "aaa1111"}, {Code: "bbb2222"}}, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinks(context.Background(), privateUpdate(7, "/links"))

		got := sender.sent()
		if len(got) != 1 {
			t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
		}
		want := linksHeader +
			"\n1. <a href=\"https://shrt.example/aaa1111\">https://shrt.example/aaa1111</a>" +
			"\n2. <a href=\"https://shrt.example/bbb2222\">https://shrt.example/bbb2222</a>"
		if got[0].text != want {
			t.Errorf("текст = %q, ожидался %q", got[0].text, want)
		}
	})

	t.Run("пусто", func(t *testing.T) {
		sender := &fakeSender{}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			return nil, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinks(context.Background(), privateUpdate(7, "/links"))

		got := sender.sent()
		if len(got) != 1 {
			t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
		}
		if got[0].text != emptyLinksText {
			t.Errorf("текст = %q, ожидался %q", got[0].text, emptyLinksText)
		}
	})
}

func TestNonPrivateIgnored(t *testing.T) {
	chatTypes := []models.ChatType{
		models.ChatTypeGroup,
		models.ChatTypeSupergroup,
		models.ChatTypeChannel,
	}
	for _, chatType := range chatTypes {
		t.Run(string(chatType), func(t *testing.T) {
			sender := &fakeSender{}
			h := newTestHandlers(fakeService{}, sender)
			update := &models.Update{Message: &models.Message{
				Chat: models.Chat{ID: 100, Type: chatType},
				From: &models.User{ID: 5},
				Text: "https://example.com/x",
			}}

			h.handleDefault(context.Background(), update)
			h.handleLinks(context.Background(), update)
			h.handleStart(context.Background(), update)

			if got := sender.sent(); len(got) != 0 {
				t.Errorf("для чата типа %q отправлено %d сообщений, ожидалось 0", chatType, len(got))
			}
		})
	}

	t.Run("без Message", func(t *testing.T) {
		sender := &fakeSender{}
		h := newTestHandlers(fakeService{}, sender)
		h.handleDefault(context.Background(), &models.Update{})
		if got := sender.sent(); len(got) != 0 {
			t.Errorf("отправлено %d сообщений, ожидалось 0", len(got))
		}
	})

	t.Run("без From", func(t *testing.T) {
		sender := &fakeSender{}
		h := newTestHandlers(fakeService{}, sender)
		update := &models.Update{Message: &models.Message{
			Chat: models.Chat{ID: 1, Type: models.ChatTypePrivate},
			Text: "https://example.com/x",
		}}
		h.handleDefault(context.Background(), update)
		if got := sender.sent(); len(got) != 0 {
			t.Errorf("отправлено %d сообщений, ожидалось 0", len(got))
		}
	})
}

func TestStartCommand(t *testing.T) {
	sender := &fakeSender{}
	h := newTestHandlers(fakeService{}, sender)

	h.handleStart(context.Background(), privateUpdate(1, "/start"))

	got := sender.sent()
	if len(got) != 1 {
		t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
	}
	if got[0].text != startText {
		t.Errorf("текст = %q, ожидался %q", got[0].text, startText)
	}
}

func TestStorageError(t *testing.T) {
	t.Run("Shorten", func(t *testing.T) {
		sender := &fakeSender{}
		svc := fakeService{shorten: func(context.Context, int64, string) (shortener.Link, error) {
			return shortener.Link{}, errors.New("db down")
		}}
		h := newTestHandlers(svc, sender)
		h.handleDefault(context.Background(), privateUpdate(1, "https://example.com/x"))

		got := sender.sent()
		if len(got) != 1 || got[0].text != storageErrorText {
			t.Errorf("сообщения = %+v, ожидался %q", got, storageErrorText)
		}
	})

	t.Run("Links", func(t *testing.T) {
		sender := &fakeSender{}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			return nil, errors.New("db down")
		}}
		h := newTestHandlers(svc, sender)
		h.handleLinks(context.Background(), privateUpdate(1, "/links"))

		got := sender.sent()
		if len(got) != 1 || got[0].text != storageErrorText {
			t.Errorf("сообщения = %+v, ожидался %q", got, storageErrorText)
		}
	})
}
