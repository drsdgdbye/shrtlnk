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

// handleLinks отвечает первой страницей персонального списка коротких ссылок на /links.
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
	h.sendList(ctx, chatID, from.ID, links, 0)
}

// handleLinksCallback обрабатывает нажатия "<"/">" пагинации списка ссылок.
// Пользователь берётся из CallbackQuery.From.ID, данные используются только для
// сверки и номера страницы.
func (h *handlers) handleLinksCallback(ctx context.Context, update *models.Update) {
	if update == nil || update.CallbackQuery == nil {
		return
	}
	cq := update.CallbackQuery

	userID, page, ok := parseLinksCallback(cq.Data)
	if !ok {
		h.logger.Debug("нераспознанные callback-данные", "data", cq.Data)
		return
	}
	if userID != cq.From.ID {
		h.logger.Debug("callback-данные чужого пользователя", "data", cq.Data, "user_id", cq.From.ID)
		return
	}
	msg := cq.Message.Message
	if msg != nil && (msg.Chat.Type != models.ChatTypePrivate || msg.Chat.ID != cq.From.ID) {
		h.logger.Debug("callback из неподходящего чата", "chat_id", msg.Chat.ID, "user_id", cq.From.ID)
		return
	}

	links, err := h.svc.Links(ctx, cq.From.ID)
	if err != nil {
		h.logger.Error("получение ссылок пользователя", "user_id", cq.From.ID, "error", err)
		h.answerCallback(ctx, cq.ID, storageErrorText)
		return
	}
	pages := linksPageCount(len(links))
	if page < 0 || page >= pages {
		h.logger.Debug("страница callback вне диапазона", "page", page, "pages", pages)
		return
	}

	h.answerCallback(ctx, cq.ID, "")
	if msg == nil {
		h.sendList(ctx, cq.From.ID, cq.From.ID, links, page)
		return
	}
	h.editList(ctx, msg.Chat.ID, cq.From.ID, msg.ID, links, page)
}

// sendList отправляет сообщение со страницей списка ссылок.
func (h *handlers) sendList(ctx context.Context, chatID, userID int64, links []shortener.Link, page int) {
	disabled := true
	_, err := h.send.SendMessage(ctx, &tgbot.SendMessageParams{
		ChatID:             chatID,
		Text:               formatLinksPage(h.base, links, page),
		ParseMode:          models.ParseModeHTML,
		LinkPreviewOptions: &models.LinkPreviewOptions{IsDisabled: &disabled},
		ReplyMarkup:        linksKeyboard(userID, page, linksPageCount(len(links))),
	})
	if err != nil {
		h.logger.Error("отправка сообщения в Telegram", "chat_id", chatID, "error", err)
	}
}

// editList правит сообщение списка на месте. Если сообщение нельзя править
// (Forbidden/NotFound), отправляет новое; прочие ошибки только логирует.
func (h *handlers) editList(ctx context.Context, chatID, userID int64, messageID int, links []shortener.Link, page int) {
	disabled := true
	_, err := h.send.EditMessageText(ctx, &tgbot.EditMessageTextParams{
		ChatID:             chatID,
		MessageID:          messageID,
		Text:               formatLinksPage(h.base, links, page),
		ParseMode:          models.ParseModeHTML,
		LinkPreviewOptions: &models.LinkPreviewOptions{IsDisabled: &disabled},
		ReplyMarkup:        linksKeyboard(userID, page, linksPageCount(len(links))),
	})
	if err == nil {
		return
	}
	if errors.Is(err, tgbot.ErrorForbidden) || errors.Is(err, tgbot.ErrorNotFound) {
		h.logger.Debug("сообщение списка нельзя отредактировать", "chat_id", chatID, "error", err)
		h.sendList(ctx, chatID, userID, links, page)
		return
	}
	h.logger.Warn("редактирование сообщения списка", "chat_id", chatID, "error", err)
}

// answerCallback отвечает на callback-запрос (пустой text — без всплывающего сообщения).
func (h *handlers) answerCallback(ctx context.Context, callbackQueryID, text string) {
	if _, err := h.send.AnswerCallbackQuery(ctx, &tgbot.AnswerCallbackQueryParams{
		CallbackQueryID: callbackQueryID,
		Text:            text,
	}); err != nil {
		h.logger.Error("ответ на callback-запрос", "error", err)
	}
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
