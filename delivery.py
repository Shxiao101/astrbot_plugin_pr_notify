"""Execute persisted QQ deliveries with bounded concurrency and explicit outcomes."""

import asyncio
import json
import sqlite3
import time

from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable
from astrbot.api import logger

TIMEOUT = 15
RETRY_DELAYS = (5, 30, 120)


class DeliveryFailed(Exception):
    """A confirmed rejection with a safe, user-visible reason."""


class UnknownResult(Exception):
    """The request may have succeeded but there is no usable acknowledgement."""


def checked_result(result, reaction=False):
    """Validate the OneBot envelope and NapCat's inner result, if present."""
    if not isinstance(result, dict):
        raise UnknownResult("接口未返回有效结果")
    if result.get("status") == "failed" or result.get("retcode", 0) != 0:
        code = result.get("retcode")
        raise DeliveryFailed(
            f"OneBot 拒绝请求（代码 {code if type(code) is int else '未知'}）"
        )
    data = result.get("data", result)
    if not isinstance(data, dict):
        raise UnknownResult("接口未返回有效数据")
    if "result" in data and data["result"] != 0:
        if reaction and data["result"] == 65002:
            return data
        code = data["result"]
        reason = "操作失败，请稍后重试" if code == 65011 else "操作被拒绝"
        raise DeliveryFailed(
            f"NapCat {reason}（代码 {code if type(code) is int else '未知'}）"
        )
    if reaction and "result" not in data:
        raise UnknownResult("贴表情接口缺少结果码")
    return data


async def call(client, action, **kwargs):
    try:
        return await asyncio.wait_for(client.call_action(action, **kwargs), TIMEOUT)
    except ActionFailed as exc:
        # Do not expose raw adapter exceptions: they can include request payloads/tokens.
        code = exc.retcode
        raise DeliveryFailed(
            f"OneBot 拒绝请求（代码 {code if type(code) is int else '未知'}）"
        ) from None
    except ApiNotAvailable:
        raise DeliveryFailed("机器人未连接或接口不可用") from None


class Dispatcher:
    """Run four workers; SQLite preserves ordering, retry deadlines and restart state."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.store = plugin.store
        self.workers = []

    def start(self):
        self.store.recover()
        self.workers = [asyncio.create_task(self.worker()) for _ in range(4)]

    async def stop(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    async def worker(self):
        last_prune = time.monotonic()
        while True:
            try:
                if time.monotonic() - last_prune > 3600:
                    self.store.prune()
                    last_prune = time.monotonic()
                task = self.store.claim()
                if task is None:
                    await asyncio.sleep(0.2)
                else:
                    await self.execute(task)
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                logger.error("[pr-notify] 队列处理失败，请检查数据库和插件日志环境")
                # Leave the interrupted row running until restart recovery. Other workers
                # must not reclaim a request whose result could not be persisted.
                await asyncio.sleep(1)

    async def execute(self, task):
        started = time.monotonic()
        try:
            try:
                client = self.plugin.client(task)
            except RuntimeError:
                raise DeliveryFailed("配置的平台不可用，请检查机器人连接") from None
            routing = {"self_id": task["self_id"]} if task["self_id"] else {}
            if task["operation"] == "reaction":
                message_id = task["message_id"]
                if task["parent"] is not None:
                    parent = self.store.task(task["parent"])
                    if parent is None or parent["status"] != "success":
                        raise DeliveryFailed("原通知未成功发送，请先处理原通知")
                    message_id = parent["message_id"]
                if not message_id:
                    raise DeliveryFailed("找不到原群消息，无法贴表情")
                result = await call(
                    client,
                    "set_msg_emoji_like",
                    message_id=int(message_id),
                    emoji_id=json.loads(task["content"]),
                    set=True,
                    **routing,
                )
                checked_result(result, reaction=True)
            else:
                key = "group_id" if task["kind"] == "group" else "user_id"
                action = (
                    "send_group_msg" if task["kind"] == "group" else "send_private_msg"
                )
                result = checked_result(
                    await call(
                        client,
                        action,
                        **{key: int(task["target"])},
                        message=json.loads(task["content"]),
                        **routing,
                    )
                )
                message_id = result.get("message_id")
                if (
                    isinstance(message_id, bool)
                    or not str(message_id).lstrip("-").isdigit()
                    or int(message_id) == 0
                ):
                    raise UnknownResult("发送响应缺少有效消息 ID，可能已送达")
                message_id = str(message_id)
            self.store.finish(
                task, "success", time.monotonic() - started, message_id=message_id
            )
        except asyncio.CancelledError:
            state = (
                ("pending" if task["attempts"] < 4 else "failed")
                if task["operation"] == "reaction"
                else "unknown"
            )
            self.store.finish(
                task, state, time.monotonic() - started, "发送中断，结果未确认"
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- unknown adapter errors must not resend a message
            definite = isinstance(exc, DeliveryFailed)
            safe_error = (
                str(exc)
                if isinstance(exc, (DeliveryFailed, UnknownResult))
                else (
                    "接口调用超时，结果未确认"
                    if isinstance(exc, TimeoutError)
                    else "连接或接口异常，结果未确认"
                )
            )
            if not definite and task["operation"] != "reaction":
                state, next_at = "unknown", 0
            elif task["attempts"] <= len(RETRY_DELAYS):
                state = "pending"
                next_at = time.time() + RETRY_DELAYS[task["attempts"] - 1]
            else:
                state, next_at = "failed", 0
            self.store.finish(
                task, state, time.monotonic() - started, safe_error, next_at
            )
