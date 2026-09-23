package telegram

import (
	"context"
	"fmt"
	"log/slog"

	tgbot "github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"

	"yt_dw/internal/shortener"
)

// shortenerService — часть сервиса сокращения, нужная боту.
type shortenerService interface {
	Shorten(ctx context.Context, userID int64, rawURL string) (shortener.Link, error)
	Links(ctx context.Context, userID int64) ([]shortener.Link, error)
}

// messageSender отправляет сообщения в Telegram. Интерфейс объявлен в пакете-потребителе,
// чтобы в тестах подменить реального бота.
type messageSender interface {
	SendMessage(ctx context.Context, params *tgbot.SendMessageParams) (*models.Message, error)
	EditMessageText(ctx context.Context, params *tgbot.EditMessageTextParams) (*models.Message, error)
	AnswerCallbackQuery(ctx context.Context, params *tgbot.AnswerCallbackQueryParams) (bool, error)
}

// Run запускает Telegram-бота в режиме long-polling и блокируется до отмены ctx.
// Ошибка инициализации (в том числе недействительный токен) возвращается сразу.
func Run(ctx context.Context, token string, svc shortenerService, base string, logger *slog.Logger) error {
	if logger == nil {
		logger = slog.Default()
	}

	var h *handlers
	b, err := tgbot.New(token,
		tgbot.WithDefaultHandler(func(ctx context.Context, _ *tgbot.Bot, update *models.Update) {
			h.handleDefault(ctx, update)
		}),
		tgbot.WithErrorsHandler(func(err error) {
			logger.Error("ошибка Telegram API", "error", err)
		}),
	)
	if err != nil {
		return fmt.Errorf("инициализация Telegram-бота: %w", err)
	}

	h = newHandlers(svc, base, logger, b)
	b.RegisterHandler(tgbot.HandlerTypeMessageText, "start", tgbot.MatchTypeCommand,
		func(ctx context.Context, _ *tgbot.Bot, update *models.Update) {
			h.handleStart(ctx, update)
		})
	b.RegisterHandler(tgbot.HandlerTypeMessageText, "links", tgbot.MatchTypeCommand,
		func(ctx context.Context, _ *tgbot.Bot, update *models.Update) {
			h.handleLinks(ctx, update)
		})
	b.RegisterHandler(tgbot.HandlerTypeCallbackQueryData, callbackPrefix, tgbot.MatchTypePrefix,
		func(ctx context.Context, _ *tgbot.Bot, update *models.Update) {
			h.handleLinksCallback(ctx, update)
		})

	if _, err := b.SetMyCommands(ctx, &tgbot.SetMyCommandsParams{
		Commands: []models.BotCommand{
			{Command: "start", Description: "Start the bot"},
			{Command: "links", Description: "Show your short links"},
		},
	}); err != nil {
		logger.Warn("не удалось зарегистрировать команды бота", "error", err)
	}

	b.Start(ctx)
	return nil
}
