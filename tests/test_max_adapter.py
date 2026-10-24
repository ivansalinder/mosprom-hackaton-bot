from types import SimpleNamespace
import unittest

from monitor.bot.max_adapter import MaxAdapter


class FakeCallbackEvent:
    def __init__(self, payload: str) -> None:
        self.callback = SimpleNamespace(
            user=SimpleNamespace(user_id=12345),
            payload=payload,
        )
        self.notifications: list[str | None] = []

    async def ack(self, notification: str | None = None) -> None:
        self.notifications.append(notification)


class MaxAdapterCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_is_acknowledged_with_notification(self) -> None:
        event = FakeCallbackEvent("reg:confirm")
        received = []

        async def callback(incoming) -> None:
            received.append(incoming)

        await MaxAdapter._handle_callback(event, callback)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].user_id, "12345")
        self.assertEqual(received[0].payload, "reg:confirm")
        self.assertEqual(event.notifications, ["Готово"])

    async def test_failed_callback_is_acknowledged_and_reraised(self) -> None:
        event = FakeCallbackEvent("reg:confirm")

        async def callback(incoming) -> None:
            raise ValueError("handler failed")

        with self.assertLogs("monitor.bot.max_adapter", level="ERROR"):
            with self.assertRaisesRegex(ValueError, "handler failed"):
                await MaxAdapter._handle_callback(event, callback)

        self.assertEqual(
            event.notifications,
            ["Не удалось выполнить действие. Попробуйте ещё раз."],
        )


if __name__ == "__main__":
    unittest.main()
