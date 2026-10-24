from dataclasses import dataclass
import logging
from typing import Awaitable, Callable

from maxapi import Bot, Dispatcher, F
from maxapi.filters.command import Command, CommandStart
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.types.updates.bot_started import BotStarted
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.types.input_media import InputMedia
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from monitor.assets import asset_stub, find_asset
from monitor.config import settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingMessage:
    user_id: str
    text: str
    attachments: tuple[object, ...] = ()


@dataclass(frozen=True)
class IncomingCallback:
    user_id: str
    payload: str


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
CallbackHandler = Callable[[IncomingCallback], Awaitable[None]]


class MaxAdapter:
    """Keeps MAX API and maxapi-specific event models out of application logic."""

    def __init__(self) -> None:
        token = (settings.MAX_BOT_TOKEN or "").strip()
        if not token:
            raise RuntimeError(
                "MAX_BOT_TOKEN is empty. Set it in the project's .env file."
            )
        self.bot = Bot(token=token)
        self.dispatcher = Dispatcher()

    @staticmethod
    def keyboard(rows: list[list[tuple[str, str]]]) -> list:
        builder = InlineKeyboardBuilder()
        for row in rows:
            builder.row(
                *[
                    CallbackButton(text=label, payload=payload)
                    for label, payload in row
                ]
            )
        return [builder.as_markup()]

    async def send(
        self,
        user_id: str,
        text: str,
        rows: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        kwargs = {"user_id": int(user_id), "text": text}
        if rows:
            kwargs["attachments"] = self.keyboard(rows)
        await self.bot.send_message(**kwargs)

    async def send_link_button(
        self, user_id: str, text: str, label: str, url: str
    ) -> None:
        builder = InlineKeyboardBuilder()
        builder.row(LinkButton(text=label, url=url))
        await self.bot.send_message(
            user_id=int(user_id),
            text=text,
            attachments=[builder.as_markup()],
        )

    async def send_asset_or_stub(self, user_id: str, asset_name: str) -> None:
        asset_path = find_asset(asset_name)
        if asset_path is None:
            if settings.SHOW_ASSET_STUBS:
                await self.send(user_id, asset_stub(asset_name))
            return
        try:
            await self.bot.send_message(
                user_id=int(user_id),
                text="",
                attachments=[InputMedia(path=str(asset_path))],
            )
        except Exception:
            logger.exception("Failed to send asset %s to user %s", asset_name, user_id)
            if settings.SHOW_ASSET_STUBS:
                try:
                    await self.send(user_id, asset_stub(asset_name))
                except Exception:
                    logger.exception(
                        "Failed to send fallback stub for asset %s to user %s",
                        asset_name,
                        user_id,
                    )

    async def send_hr_notice(
        self, text: str, fallback_user_id: str | None = None
    ) -> None:
        chat_id = (settings.HR_ESCALATION_CHAT_ID or "").strip()
        if not chat_id:
            if settings.DEMO_MODE and fallback_user_id:
                await self.send(fallback_user_id, f"[HR-уведомление, демо] {text}")
            else:
                logger.warning("HR notification is a stub: %s", text)
            return
        await self.bot.send_message(chat_id=int(chat_id), text=text)

    async def send_self_intro(
        self,
        user_id: str,
        employee_code: str,
        text: str,
        attachments: tuple[object, ...],
    ) -> None:
        notice = f"Самопредставление сотрудника {employee_code}: {text or '[фото]'}"
        chat_id = (settings.HR_ESCALATION_CHAT_ID or "").strip()
        if chat_id:
            await self.bot.send_message(
                chat_id=int(chat_id),
                text=notice,
                attachments=list(attachments) or None,
            )
        elif settings.DEMO_MODE:
            await self.bot.send_message(
                user_id=int(user_id),
                text=f"[HR-уведомление, демо] {notice}",
                attachments=list(attachments) or None,
            )
        else:
            logger.warning("Self-introduction delivery is a stub: %s", notice)

    async def send_head_hr_notice(
        self, text: str, fallback_user_id: str | None = None
    ) -> None:
        chat_id = (settings.HEAD_HR_CHAT_ID or "").strip()
        if not chat_id:
            if settings.DEMO_MODE and fallback_user_id:
                await self.send(fallback_user_id, f"[HR-уведомление, демо] {text}")
            else:
                logger.warning("Weekly digest delivery is a stub: %s", text)
            return
        await self.bot.send_message(chat_id=int(chat_id), text=text)

    def bind(
        self,
        *,
        start: MessageHandler,
        message: MessageHandler,
        callback: CallbackHandler,
        bot_started: MessageHandler,
    ) -> None:
        @self.dispatcher.message_created(CommandStart())
        async def _start(event: MessageCreated) -> None:
            await start(self._message(event))

        @self.dispatcher.message_created(Command("Food"))
        async def _food(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(Command("HR"))
        async def _hr(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(Command("close"))
        async def _close(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(Command("simulate"))
        async def _simulate(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(Command("approve"))
        async def _approve(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(Command("export_analytics"))
        async def _export(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(F.message.body.text)
        async def _text(event: MessageCreated) -> None:
            await message(self._message(event))

        @self.dispatcher.message_created(F.message.body.attachments)
        async def _attachment(event: MessageCreated) -> None:
            body = event.message.body
            if body and body.text:
                return
            await message(self._message(event))

        @self.dispatcher.message_callback()
        async def _callback(event: MessageCallback) -> None:
            await self._handle_callback(event, callback)

        @self.dispatcher.bot_started()
        async def _bot_started(event: BotStarted) -> None:
            await bot_started(
                IncomingMessage(user_id=str(event.user.user_id), text="/start")
            )

    @staticmethod
    async def _handle_callback(
        event: MessageCallback, callback: CallbackHandler
    ) -> None:
        user_id = str(event.callback.user.user_id)
        payload = event.callback.payload or ""
        notification = "Готово"
        try:
            await callback(IncomingCallback(user_id=user_id, payload=payload))
        except Exception:
            notification = "Не удалось выполнить действие. Попробуйте ещё раз."
            logger.exception("Callback handler failed (payload=%r)", payload)
            raise
        finally:
            try:
                await event.ack(notification=notification)
            except Exception as error:
                logger.warning("Failed to acknowledge callback: %s", error)

    @staticmethod
    def _message(event: MessageCreated) -> IncomingMessage:
        sender = event.message.sender
        return IncomingMessage(
            user_id=str(sender.user_id) if sender else "",
            text=(
                event.message.body.text.strip()
                if event.message.body and event.message.body.text
                else ""
            ),
            attachments=tuple(event.message.body.attachments or ())
            if event.message.body
            else (),
        )

    async def run(self) -> None:
        await self.dispatcher.start_polling(self.bot)

    async def close(self) -> None:
        await self.bot.close_session()
