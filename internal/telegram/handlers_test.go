package telegram

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	tgbot "github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"

	"yt_dw/internal/shortener"
)

const testBase = "https://shrt.example"

type sentMessage struct {
	chatID      any
	text        string
	parseMode   models.ParseMode
	disabled    bool
	replyMarkup models.ReplyMarkup
}

type editedMessage struct {
	chatID      any
	messageID   int
	text        string
	parseMode   models.ParseMode
	disabled    bool
	replyMarkup models.ReplyMarkup
}

type answeredCallback struct {
	callbackQueryID string
	text            string
}

type fakeSender struct {
	mu       sync.Mutex
	messages []sentMessage
	edits    []editedMessage
	answers  []answeredCallback
	editErr  error
}

func (f *fakeSender) SendMessage(_ context.Context, params *tgbot.SendMessageParams) (*models.Message, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.messages = append(f.messages, sentMessage{
		chatID:      params.ChatID,
		text:        params.Text,
		parseMode:   params.ParseMode,
		disabled:    linkPreviewDisabled(params.LinkPreviewOptions),
		replyMarkup: params.ReplyMarkup,
	})
	return &models.Message{}, nil
}

func (f *fakeSender) EditMessageText(_ context.Context, params *tgbot.EditMessageTextParams) (*models.Message, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.editErr != nil {
		return nil, f.editErr
	}
	f.edits = append(f.edits, editedMessage{
		chatID:      params.ChatID,
		messageID:   params.MessageID,
		text:        params.Text,
		parseMode:   params.ParseMode,
		disabled:    linkPreviewDisabled(params.LinkPreviewOptions),
		replyMarkup: params.ReplyMarkup,
	})
	return &models.Message{}, nil
}

func (f *fakeSender) AnswerCallbackQuery(_ context.Context, params *tgbot.AnswerCallbackQueryParams) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.answers = append(f.answers, answeredCallback{
		callbackQueryID: params.CallbackQueryID,
		text:            params.Text,
	})
	return true, nil
}

func (f *fakeSender) sent() []sentMessage {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]sentMessage, len(f.messages))
	copy(out, f.messages)
	return out
}

func (f *fakeSender) edited() []editedMessage {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]editedMessage, len(f.edits))
	copy(out, f.edits)
	return out
}

func (f *fakeSender) answered() []answeredCallback {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]answeredCallback, len(f.answers))
	copy(out, f.answers)
	return out
}

func linkPreviewDisabled(opts *models.LinkPreviewOptions) bool {
	return opts != nil && opts.IsDisabled != nil && *opts.IsDisabled
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

// callbackUpdate собирает нажатие кнопки с доступным сообщением.
func callbackUpdate(fromID, chatID int64, chatType models.ChatType, messageID int, data string) *models.Update {
	return &models.Update{CallbackQuery: &models.CallbackQuery{
		ID:   "cq1",
		From: models.User{ID: fromID},
		Message: models.MaybeInaccessibleMessage{
			Message: &models.Message{
				ID:   messageID,
				Chat: models.Chat{ID: chatID, Type: chatType},
			},
		},
		Data: data,
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
	t.Run("первая страница из 10 строк", func(t *testing.T) {
		links := pagesLinks(23)
		sender := &fakeSender{}
		svc := fakeService{links: func(_ context.Context, userID int64) ([]shortener.Link, error) {
			if userID != 7 {
				t.Errorf("userID = %d, ожидался 7", userID)
			}
			return links, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinks(context.Background(), privateUpdate(7, "/links"))

		got := sender.sent()
		if len(got) != 1 {
			t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
		}
		if want := formatLinksPage(testBase, links, 0); got[0].text != want {
			t.Errorf("текст = %q, ожидался %q", got[0].text, want)
		}
		if anchors := strings.Count(got[0].text, "<a href="); anchors != linksPageSize {
			t.Errorf("анкоров = %d, ожидалось %d", anchors, linksPageSize)
		}
		if !strings.Contains(got[0].text, ">Название 1</a>") || !strings.Contains(got[0].text, ">Название 10</a>") {
			t.Errorf("нет названий первых десяти ссылок: %q", got[0].text)
		}
		if strings.Contains(got[0].text, "Название 11") {
			t.Errorf("первая страница содержит одиннадцатую ссылку: %q", got[0].text)
		}
		keyboard, ok := got[0].replyMarkup.(models.InlineKeyboardMarkup)
		if !ok {
			t.Fatalf("ReplyMarkup = %#v, ожидалась InlineKeyboardMarkup", got[0].replyMarkup)
		}
		if len(keyboard.InlineKeyboard) != 1 || len(keyboard.InlineKeyboard[0]) != 1 {
			t.Fatalf("клавиатура = %#v, ожидалась одна кнопка", keyboard.InlineKeyboard)
		}
		if want := encodeLinksCallback(7, 1); keyboard.InlineKeyboard[0][0].CallbackData != want {
			t.Errorf("callback_data = %q, ожидалось %q", keyboard.InlineKeyboard[0][0].CallbackData, want)
		}
		if got[0].parseMode != models.ParseModeHTML || !got[0].disabled {
			t.Errorf("ParseMode/превью = %q/%v, ожидались HTML и отключённое превью", got[0].parseMode, got[0].disabled)
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
		if got[0].replyMarkup != nil {
			t.Errorf("ReplyMarkup = %#v, ожидался nil", got[0].replyMarkup)
		}
	})

	t.Run("одна страница без клавиатуры", func(t *testing.T) {
		links := pagesLinks(linksPageSize)
		sender := &fakeSender{}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			return links, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinks(context.Background(), privateUpdate(7, "/links"))

		got := sender.sent()
		if len(got) != 1 {
			t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
		}
		if got[0].replyMarkup != nil {
			t.Errorf("ReplyMarkup = %#v, ожидался nil при одной странице", got[0].replyMarkup)
		}
	})

	t.Run("без заголовка показывается домен", func(t *testing.T) {
		links := []shortener.Link{{Code: "aaa1111", URL: "https://www.example.com/page"}}
		sender := &fakeSender{}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			return links, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinks(context.Background(), privateUpdate(7, "/links"))

		got := sender.sent()
		if len(got) != 1 {
			t.Fatalf("отправлено %d сообщений, ожидалось 1", len(got))
		}
		want := linksHeader + "\n1. <a href=\"https://shrt.example/aaa1111\">example.com</a>"
		if got[0].text != want {
			t.Errorf("текст = %q, ожидался %q", got[0].text, want)
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

func TestLinksCallbackValid(t *testing.T) {
	links := pagesLinks(23)
	sender := &fakeSender{}
	svc := fakeService{links: func(_ context.Context, userID int64) ([]shortener.Link, error) {
		if userID != 7 {
			t.Errorf("userID = %d, ожидался 7", userID)
		}
		return links, nil
	}}
	h := newTestHandlers(svc, sender)

	h.handleLinksCallback(context.Background(), callbackUpdate(7, 7, models.ChatTypePrivate, 55, encodeLinksCallback(7, 1)))

	answers := sender.answered()
	if len(answers) != 1 {
		t.Fatalf("ответов на callback = %d, ожидался 1", len(answers))
	}
	if answers[0].callbackQueryID != "cq1" {
		t.Errorf("CallbackQueryID = %q, ожидался %q", answers[0].callbackQueryID, "cq1")
	}
	if answers[0].text != "" {
		t.Errorf("текст ответа = %q, ожидался пустой", answers[0].text)
	}

	edits := sender.edited()
	if len(edits) != 1 {
		t.Fatalf("правок сообщения = %d, ожидалась 1", len(edits))
	}
	if want := formatLinksPage(testBase, links, 1); edits[0].text != want {
		t.Errorf("текст правки = %q, ожидался %q", edits[0].text, want)
	}
	if edits[0].chatID != int64(7) || edits[0].messageID != 55 {
		t.Errorf("правка = %+v, ожидались чат 7 и сообщение 55", edits[0])
	}
	if edits[0].parseMode != models.ParseModeHTML || !edits[0].disabled {
		t.Errorf("ParseMode/превью = %q/%v, ожидались HTML и отключённое превью", edits[0].parseMode, edits[0].disabled)
	}
	keyboard, ok := edits[0].replyMarkup.(models.InlineKeyboardMarkup)
	if !ok {
		t.Fatalf("ReplyMarkup = %#v, ожидалась InlineKeyboardMarkup", edits[0].replyMarkup)
	}
	if len(keyboard.InlineKeyboard) != 1 || len(keyboard.InlineKeyboard[0]) != 2 {
		t.Fatalf("клавиатура = %#v, ожидались обе кнопки", keyboard.InlineKeyboard)
	}
	if want := encodeLinksCallback(7, 0); keyboard.InlineKeyboard[0][0].CallbackData != want {
		t.Errorf("назад = %q, ожидалось %q", keyboard.InlineKeyboard[0][0].CallbackData, want)
	}
	if want := encodeLinksCallback(7, 2); keyboard.InlineKeyboard[0][1].CallbackData != want {
		t.Errorf("вперёд = %q, ожидалось %q", keyboard.InlineKeyboard[0][1].CallbackData, want)
	}
}

func TestLinksCallbackOutOfRange(t *testing.T) {
	links := pagesLinks(11) // две страницы

	for _, page := range []int{-1, 2, 99} {
		t.Run(fmt.Sprintf("страница %d", page), func(t *testing.T) {
			sender := &fakeSender{}
			svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
				return links, nil
			}}
			h := newTestHandlers(svc, sender)

			h.handleLinksCallback(context.Background(), callbackUpdate(7, 7, models.ChatTypePrivate, 55, encodeLinksCallback(7, page)))

			if got := sender.answered(); len(got) != 0 {
				t.Errorf("ответов на callback = %d, ожидалось 0", len(got))
			}
			if got := sender.edited(); len(got) != 0 {
				t.Errorf("правок = %d, ожидалось 0", len(got))
			}
			if got := sender.sent(); len(got) != 0 {
				t.Errorf("новых сообщений = %d, ожидалось 0", len(got))
			}
		})
	}
}

func TestLinksCallbackForgedUser(t *testing.T) {
	tests := []struct {
		name     string
		fromID   int64
		chatID   int64
		chatType models.ChatType
		data     string
	}{
		{name: "чужой user_id", fromID: 7, chatID: 7, chatType: models.ChatTypePrivate, data: encodeLinksCallback(8, 1)},
		{name: "чужой личный чат", fromID: 7, chatID: 8, chatType: models.ChatTypePrivate, data: encodeLinksCallback(7, 1)},
		{name: "групповой чат", fromID: 7, chatID: 7, chatType: models.ChatTypeGroup, data: encodeLinksCallback(7, 1)},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			sender := &fakeSender{}
			svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
				t.Error("Links не должен вызываться для неавторизованного нажатия")
				return nil, nil
			}}
			h := newTestHandlers(svc, sender)

			h.handleLinksCallback(context.Background(), callbackUpdate(tt.fromID, tt.chatID, tt.chatType, 55, tt.data))

			if got := sender.answered(); len(got) != 0 {
				t.Errorf("ответов на callback = %d, ожидалось 0", len(got))
			}
			if got := sender.edited(); len(got) != 0 {
				t.Errorf("правок = %d, ожидалось 0", len(got))
			}
			if got := sender.sent(); len(got) != 0 {
				t.Errorf("новых сообщений = %d, ожидалось 0", len(got))
			}
		})
	}
}

func TestLinksCallbackStorageError(t *testing.T) {
	sender := &fakeSender{}
	svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
		return nil, errors.New("db down")
	}}
	h := newTestHandlers(svc, sender)

	h.handleLinksCallback(context.Background(), callbackUpdate(7, 7, models.ChatTypePrivate, 55, encodeLinksCallback(7, 0)))

	answers := sender.answered()
	if len(answers) != 1 || answers[0].text != storageErrorText {
		t.Errorf("ответы = %+v, ожидался один с %q", answers, storageErrorText)
	}
	if got := sender.edited(); len(got) != 0 {
		t.Errorf("правок = %d, ожидалось 0", len(got))
	}
	if got := sender.sent(); len(got) != 0 {
		t.Errorf("новых сообщений = %d, ожидалось 0", len(got))
	}
}

func TestLinksCallbackInaccessibleMessage(t *testing.T) {
	links := pagesLinks(3)
	sender := &fakeSender{}
	svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
		return links, nil
	}}
	h := newTestHandlers(svc, sender)

	update := &models.Update{CallbackQuery: &models.CallbackQuery{
		ID:   "cq1",
		From: models.User{ID: 7},
		Data: encodeLinksCallback(7, 0),
	}}
	h.handleLinksCallback(context.Background(), update)

	answers := sender.answered()
	if len(answers) != 1 || answers[0].text != "" {
		t.Errorf("ответы = %+v, ожидался один пустой", answers)
	}
	if got := sender.edited(); len(got) != 0 {
		t.Errorf("правок = %d, ожидалось 0", len(got))
	}
	got := sender.sent()
	if len(got) != 1 {
		t.Fatalf("новых сообщений = %d, ожидалось 1", len(got))
	}
	if got[0].chatID != int64(7) {
		t.Errorf("чат нового сообщения = %v, ожидался 7", got[0].chatID)
	}
	if want := formatLinksPage(testBase, links, 0); got[0].text != want {
		t.Errorf("текст нового сообщения = %q, ожидался %q", got[0].text, want)
	}
}

func TestLinksCallbackIgnoredInput(t *testing.T) {
	t.Run("без callback", func(t *testing.T) {
		sender := &fakeSender{}
		h := newTestHandlers(fakeService{}, sender)
		h.handleLinksCallback(context.Background(), &models.Update{})
		if got := sender.sent(); len(got) != 0 {
			t.Errorf("отправлено %d сообщений, ожидалось 0", len(got))
		}
	})

	t.Run("мусорные данные", func(t *testing.T) {
		sender := &fakeSender{}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			t.Error("Links не должен вызываться для мусорных данных")
			return nil, nil
		}}
		h := newTestHandlers(svc, sender)
		h.handleLinksCallback(context.Background(), callbackUpdate(7, 7, models.ChatTypePrivate, 55, "garbage"))

		if got := sender.answered(); len(got) != 0 {
			t.Errorf("ответов на callback = %d, ожидалось 0", len(got))
		}
		if got := sender.edited(); len(got) != 0 {
			t.Errorf("правок = %d, ожидалось 0", len(got))
		}
	})
}

func TestLinksCallbackEditError(t *testing.T) {
	links := pagesLinks(3)

	t.Run("Forbidden — новое сообщение", func(t *testing.T) {
		sender := &fakeSender{editErr: tgbot.ErrorForbidden}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			return links, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinksCallback(context.Background(), callbackUpdate(7, 7, models.ChatTypePrivate, 55, encodeLinksCallback(7, 0)))

		if got := sender.answered(); len(got) != 1 {
			t.Errorf("ответов = %d, ожидался 1", len(got))
		}
		if got := sender.edited(); len(got) != 0 {
			t.Errorf("правок = %d, ожидалось 0", len(got))
		}
		if got := sender.sent(); len(got) != 1 {
			t.Errorf("новых сообщений = %d, ожидалось 1", len(got))
		}
	})

	t.Run("прочая ошибка — без дублирования", func(t *testing.T) {
		sender := &fakeSender{editErr: errors.New("boom")}
		svc := fakeService{links: func(context.Context, int64) ([]shortener.Link, error) {
			return links, nil
		}}
		h := newTestHandlers(svc, sender)

		h.handleLinksCallback(context.Background(), callbackUpdate(7, 7, models.ChatTypePrivate, 55, encodeLinksCallback(7, 0)))

		if got := sender.answered(); len(got) != 1 {
			t.Errorf("ответов = %d, ожидался 1", len(got))
		}
		if got := sender.sent(); len(got) != 0 {
			t.Errorf("новых сообщений = %d, ожидалось 0", len(got))
		}
	})
}
