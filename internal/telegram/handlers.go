package telegram

import (
	"context"
	"errors"
	"log/slog"

	tgbot "github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"

	"yt_dw/internal/shortener"
)

// handlers объединяет зависимости обработчиков Telegram-апдейтов.
type handlers struct {
	svc    shortenerService
	base   string
	logger *slog.Logger
	send   messageSender
}

// newHandlers создаёт набор обработчиков.
func newHandlers(svc shortenerService, base string, logger *slog.Logger, sender messageSender) *handlers {
	if logger == nil {
		logger = slog.Default()
	}
	return &handlers{
		svc:    svc,
		base:   base,
		logger: logger,
		send:   sender,
	}
}

// privateContext проверяет, что апдейт пришёл из личного чата и содержит отправителя.
// Возвращает отправителя и идентификатор чата; false означает, что апдейт надо игнорировать.
func privateContext(update *models.Update) (*models.User, int64, bool) {
	if update == nil || update.Message == nil {
		return nil, 0, false
	}
	msg := update.Message
	if msg.Chat.Type != models.ChatTypePrivate || msg.From == nil {
		return nil, 0, false
	}
	return msg.From, msg.Chat.ID, true
}

// handleStart отвечает приветствием на /start.
func (h *handlers) handleStart(ctx context.Context, update *models.Update) {
	if _, chatID, ok := privateContext(update); ok {
		h.sendText(ctx, chatID, startText)
	}
}

// handleLinks отвечает персональным списком коротких ссылок на /links.
func (h *handlers) handleLinks(ctx context.Context, update *models.Update) {
	from, chatID, ok := privateContext(update)
	if !ok {
		return
	}
	links, err := h.svc.Links(ctx, from.ID)
	if err != nil {
		h.logger.Error("получение ссылок пользователя", "user_id", from.ID, "error", err)
		h.sendText(ctx, chatID, storageErrorText)
		return
	}
	h.sendText(ctx, chatID, formatLinks(h.base, links))
}

// handleDefault обрабатывает всё, что не совпало с командами: валидные ссылки и мусор.
func (h *handlers) handleDefault(ctx context.Context, update *models.Update) {
	from, chatID, ok := privateContext(update)
	if !ok {
		return
	}
	if update.Message.Text == "" {
		h.sendText(ctx, chatID, fallbackText)
		return
	}

	link, err := h.svc.Shorten(ctx, from.ID, update.Message.Text)
	if err != nil {
		if errors.Is(err, shortener.ErrInvalidURL) {
			h.sendText(ctx, chatID, fallbackText)
			return
		}
		h.logger.Error("сокращение ссылки", "user_id", from.ID, "error", err)
		h.sendText(ctx, chatID, storageErrorText)
		return
	}
	h.sendText(ctx, chatID, formatShort(absoluteShortURL(h.base, link.Code)))
}

// sendText отправляет HTML-сообщение без превью ссылок.
func (h *handlers) sendText(ctx context.Context, chatID int64, text string) {
	disabled := true
	_, err := h.send.SendMessage(ctx, &tgbot.SendMessageParams{
		ChatID:             chatID,
		Text:               text,
		ParseMode:          models.ParseModeHTML,
		LinkPreviewOptions: &models.LinkPreviewOptions{IsDisabled: &disabled},
	})
	if err != nil {
		h.logger.Error("отправка сообщения в Telegram", "chat_id", chatID, "error", err)
	}
}
