"""Long-lived PR plugin contracts: signed HTTP, QQ routing and persisted state.

Owned by this plugin. AstrBot/QQ are faked; aiohttp and SQLite run for real.
"""

import asyncio
import hashlib
import hmac
import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiocqhttp.exceptions import ActionFailed
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq):
        self.qq = qq


class Star:
    def __init__(self, context):
        self.context = context


class Event:
    def __init__(self, text, group="100", admin=True, mentions=()):
        self.components = [Plain(text), *(At(qq) for qq in mentions)]
        self.group, self.admin = group, admin

    def get_messages(self):
        return self.components

    def stop_event(self):
        pass

    def is_admin(self):
        return self.admin

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return "qq-main"

    def get_self_id(self):
        return "999"

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "123"

    def plain_result(self, text):
        return text


class Bot:
    def __init__(self):
        self.calls = []
        self.fail_user = None

    async def call_action(self, action, **kwargs):
        if self.fail_user is not None and kwargs.get("user_id") == self.fail_user:
            raise ActionFailed({"retcode": 100})
        self.calls.append((action, kwargs))
        return (
            {"result": 0}
            if action == "set_msg_emoji_like"
            else {"message_id": len(self.calls)}
        )


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_secret_is_generated_persisted_and_reused(self):
        class SavedConfig(dict):
            def save_config(self):
                self.saved = dict(self)

        await self.plugin.terminate()
        config = SavedConfig(self.config)
        config["webhook_secret"] = ""
        self.plugin = self.module.PrNotify(self.context, config)
        await self.plugin.initialize()
        secret = config.saved["webhook_secret"]
        self.assertGreaterEqual(len(secret), 32)
        await self.plugin.terminate()
        restored = SavedConfig(config.saved)
        self.plugin = self.module.PrNotify(self.context, restored)
        await self.plugin.initialize()
        self.assertEqual(restored["webhook_secret"], secret)
        self.assertFalse(hasattr(restored, "saved"))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = Bot()
        platform = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(name="aiocqhttp"),
            get_client=lambda: self.bot,
        )
        self.context = types.SimpleNamespace(
            register_web_api=lambda *args: None,
            get_platform_inst=lambda name: platform if name == "qq-main" else None,
        )
        modules = {}
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.star",
            "astrbot.api.web",
        ):
            modules[name] = types.ModuleType(name)
        modules["astrbot.api.web"].request = types.SimpleNamespace()
        modules["astrbot.api.web"].json_response = lambda data: data
        modules["astrbot.api.web"].error_response = lambda message: {
            "status": "error",
            "message": message,
        }
        modules["astrbot.api"].AstrBotConfig = dict
        modules["astrbot.api"].logger = logging.getLogger("test.pr-notify")
        modules["astrbot.api.event"].AstrMessageEvent = Event
        modules["astrbot.api.event"].filter = types.SimpleNamespace(
            command=lambda name: lambda fn: fn
        )
        modules["astrbot.api.message_components"].At = At
        modules["astrbot.api.message_components"].Plain = Plain
        modules["astrbot.api.star"].Context = object
        modules["astrbot.api.star"].Star = Star
        modules["astrbot.api.star"].StarTools = types.SimpleNamespace(
            get_data_dir=lambda name: Path(self.temp.name)
        )
        self.modules_patch = patch.dict(sys.modules, modules)
        self.modules_patch.start()
        plugin_dir = Path(__file__).resolve().parents[1]
        self.path_patch = patch.object(sys, "path", [str(plugin_dir.parent), *sys.path])
        self.path_patch.start()
        self.module = importlib.import_module(f"{plugin_dir.name}.main")
        # Reload so each test uses its own data directory.
        self.module = importlib.reload(self.module)
        schema = json.loads(
            (plugin_dir / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.config = {key: value["default"] for key, value in schema.items()}
        self.config.update(
            webhook_secret="test-secret",
            webhook_host="127.0.0.1",
            webhook_port=0,
            merged_reaction_emoji_id="76",
            closed_reaction_emoji_id="100",
        )
        self.plugin = self.module.PrNotify(self.context, self.config)
        await self.plugin.initialize()
        app = web.Application()
        app.router.add_post("/github/webhook", self.plugin.webhook)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()
        await self.plugin.terminate()
        self.path_patch.stop()
        self.modules_patch.stop()
        self.temp.cleanup()

    async def command(self, text, **kwargs):
        return "\n".join([r async for r in self.plugin.notify(Event(text, **kwargs))])

    async def post(
        self,
        event,
        payload,
        delivery="delivery-1",
        signature=None,
        secret="test-secret",
    ):
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature
            or "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        }
        response = await self.http.post("/github/webhook", data=body, headers=headers)
        await response.read()
        await asyncio.sleep(0.25)
        return response.status

    async def bind(self, group="100"):
        await self.command("/notify repo add org/repo", group=group)
        self.assertEqual(
            await self.post("ping", {"repository": {"full_name": "org/repo"}}), 200
        )
        await self.command("/notify owner add org/repo 111 222", group=group)
        self.bot.calls.clear()

    def payload(self, action="opened", merged=False):
        return {
            "repository": {"full_name": "org/repo"},
            "action": action,
            "pull_request": {
                "number": 7,
                "title": "新资料",
                "html_url": "https://github.com/org/repo/pull/7",
                "user": {"login": "alice"},
                "merged": merged,
            },
        }

    async def test_signature_and_payload_boundary(self):
        await self.command("/notify repo add org/repo")
        payload = {"repository": {"full_name": "org/repo"}}
        self.assertEqual(await self.post("ping", payload, signature="sha256=bad"), 401)
        self.assertEqual(await self.post("ping", []), 400)
        self.assertEqual(await self.post("pull_request", payload), 400)
        self.assertEqual(
            await self.command("/notify repo list"), "当前会话未监听任何仓库"
        )

    async def test_repository_secrets_isolate_signatures_and_survive_reload(self):
        """Long-lived HTTP contract: dedicated keys cannot authorize other repositories."""
        await self.bind()
        self.config["repositories"] = [
            {
                "enabled": True,
                "repository": name,
                "platform_id": "qq-main",
                "bot_qq": "",
                "group_id": "",
                "mode": "private",
                "owners": ["333"],
                **fields,
            }
            for name, fields in [
                ("Panel/First", {"webhook_secret": "first-key"}),
                ("panel/second", {"webhook_secret": "second-key"}),
                ("panel/legacy", {}),
            ]
        ]
        await self.http.close()
        for _ in range(2):
            await self.plugin.terminate()
            self.plugin = self.module.PrNotify(self.context, self.config)
            await self.plugin.initialize()
        app = web.Application()
        app.router.add_post("/github/webhook", self.plugin.webhook)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()

        for name, key in [
            ("PANEL/FIRST", "first-key"),
            ("panel/second", "second-key"),
            ("panel/legacy", "test-secret"),
            ("org/repo", "test-secret"),
        ]:
            payload = self.payload()
            payload["repository"]["full_name"] = name
            for candidate in ("first-key", "second-key", "test-secret"):
                with self.subTest(repository=name, key=candidate):
                    self.assertEqual(
                        await self.post(
                            "pull_request",
                            payload,
                            f"{name}-{candidate}",
                            secret=candidate,
                        ),
                        200 if candidate == key else 401,
                    )
        status = await self.plugin.dashboard.status()
        encoded = json.dumps(status)
        for key in ("first-key", "second-key", "test-secret"):
            self.assertNotIn(key, encoded)
        self.assertEqual(len(self.bot.calls), 4)
        before = encoded
        payload = self.payload()
        payload["repository"]["full_name"] = "panel/second"
        self.assertEqual(await self.post("ping", payload, secret="first-key"), 401)
        self.assertEqual(json.dumps(await self.plugin.dashboard.status()), before)

    async def test_binding_scope_timeout_and_permissions(self):
        self.assertIn(
            "仅 AstrBot 管理员", await self.command("/notify repo add", admin=False)
        )
        await self.command("/notify repo add org/repo")
        self.assertIn("另一个会话", await self.command("/notify repo add", group="200"))
        await self.post("ping", {"repository": {"full_name": "other/repo"}})
        self.assertIsNone(self.plugin.store.get_repo("other/repo"))
        self.plugin.pending["expires"] = 0
        await self.post("ping", {"repository": {"full_name": "org/repo"}})
        self.assertIsNone(self.plugin.store.get_repo("org/repo"))
        await self.bind()
        self.assertIn(
            "未监听", await self.command("/notify repo remove org/repo", group="200")
        )
        self.assertIn("org/repo", await self.command("/notify repo list"))

    async def test_owner_mentions_and_mode_routing(self):
        await self.bind()
        await self.command("/notify owner add org/repo", mentions=("333", "999"))
        self.assertIn("333", await self.command("/notify owner list org/repo"))
        self.assertNotIn("999", await self.command("/notify owner list org/repo"))
        await self.command("/notify owner remove org/repo 333")
        for mode, actions in [
            ("group", ["send_group_msg"]),
            ("private", ["send_private_msg"] * 2),
            ("both", ["send_group_msg", "send_private_msg", "send_private_msg"]),
        ]:
            await self.command(f"/notify mode org/repo {mode}")
            self.bot.calls.clear()
            self.assertEqual(await self.post("pull_request", self.payload(), mode), 200)
            self.assertEqual([a for a, _ in self.bot.calls], actions)
            for action, params in self.bot.calls:
                self.assertEqual(params["self_id"], "999")
                self.assertEqual(
                    sum(s["type"] == "at" for s in params["message"]),
                    2 if action == "send_group_msg" else 0,
                )

    async def test_partial_failure_redelivery_and_restart(self):
        await self.bind()
        await self.command("/notify mode org/repo both")
        self.bot.fail_user = 111
        self.assertEqual(await self.post("pull_request", self.payload()), 200)
        self.assertEqual(len(self.bot.calls), 2)
        await self.plugin.terminate()
        self.plugin = self.module.PrNotify(self.context, self.config)
        await self.plugin.initialize()
        # The HTTP route remains attached to the previous instance, so restart it too.
        await self.http.close()
        app = web.Application()
        app.router.add_post("/github/webhook", self.plugin.webhook)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.bot.fail_user = None
        with self.plugin.store.db:
            self.plugin.store.db.execute(
                "UPDATE tasks SET next_at=0 WHERE status='pending'"
            )
        self.assertEqual(await self.post("pull_request", self.payload()), 200)
        self.assertEqual(len(self.bot.calls), 3)
        self.assertEqual(await self.post("pull_request", self.payload()), 200)
        self.assertEqual(len(self.bot.calls), 3)
        self.assertEqual(
            await self.post("pull_request", self.payload("closed", True), "close"), 200
        )
        self.assertEqual(
            [p["emoji_id"] for a, p in self.bot.calls if a == "set_msg_emoji_like"],
            ["76"],
        )

    async def test_reopen_reacts_to_new_message_and_remove_cleans_state(self):
        await self.bind()
        await self.post("pull_request", self.payload(), "open")
        await self.post("pull_request", self.payload("closed"), "close")
        await self.post("pull_request", self.payload("reopened"), "reopen")
        await self.post("pull_request", self.payload("closed", True), "merge")
        reactions = [p for a, p in self.bot.calls if a == "set_msg_emoji_like"]
        self.assertEqual(
            [(p["message_id"], p["emoji_id"]) for p in reactions],
            [(1, "100"), (3, "76")],
        )
        await self.command("/notify repo remove org/repo")
        count = len(self.bot.calls)
        await self.post("pull_request", self.payload("closed"), "late-close")
        self.assertEqual(len(self.bot.calls), count)
        self.assertEqual(self.plugin.store.owners("org/repo"), [])

    async def test_private_binding_and_no_owner_no_notification(self):
        await self.bind(group="")
        self.assertIn(
            "仅支持 private", await self.command("/notify mode org/repo both", group="")
        )
        await self.post("pull_request", self.payload())
        self.assertEqual([a for a, _ in self.bot.calls], ["send_private_msg"] * 2)
        # Older versions persisted private messages; they must also be skipped.
        self.plugin.store.remember(
            "org/repo", "legacy-private", "private:111", (7, "private", "111", "99")
        )
        for merged in (False, True):
            self.assertEqual(
                await self.post(
                    "pull_request",
                    self.payload("closed", merged),
                    f"private-close-{merged}",
                ),
                200,
            )
        self.assertEqual([a for a, _ in self.bot.calls], ["send_private_msg"] * 2)
        await self.command("/notify owner remove org/repo 111 222", group="")
        self.bot.calls.clear()
        await self.post("pull_request", self.payload(), "next")
        self.assertEqual(self.bot.calls, [])

    async def test_panel_config_updates_removes_and_preserves_command_repos(self):
        await self.bind()
        await self.plugin.terminate()
        entry = {
            "enabled": True,
            "repository": "Panel/Repo",
            "platform_id": "qq-main",
            "bot_qq": "",
            "group_id": "",
            "mode": "private",
            "owners": ["333", "333"],
        }
        self.config["repositories"] = [entry]
        self.plugin = self.module.PrNotify(self.context, self.config)
        await self.plugin.initialize()
        await self.plugin.pull_request(
            "panel/repo", "panel-open", "opened", self.payload()["pull_request"]
        )
        await asyncio.sleep(0.25)
        self.assertEqual(self.bot.calls[-1][1]["user_id"], 333)
        self.assertNotIn("self_id", self.bot.calls[-1][1])
        self.assertIn("面板", await self.command("/notify owner add panel/repo 444"))
        await self.plugin.terminate()
        entry.update(group_id="500", mode="both", owners=["444"])
        self.plugin = self.module.PrNotify(self.context, self.config)
        await self.plugin.initialize()
        self.assertEqual(self.plugin.store.owners("panel/repo"), ["444"])
        self.assertEqual(self.plugin.store.messages("panel/repo", 7), [])
        await self.plugin.terminate()
        self.config["repositories"] = []
        self.plugin = self.module.PrNotify(self.context, self.config)
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.store.get_repo("panel/repo"))
        self.assertIsNotNone(self.plugin.store.get_repo("org/repo"))

    async def test_invalid_panel_config_leaves_previous_routes_untouched(self):
        await self.bind()
        await self.plugin.terminate()
        self.config["repositories"] = [
            {
                "enabled": True,
                "repository": "org/repo",
                "platform_id": "qq-main",
                "bot_qq": "",
                "group_id": "",
                "mode": "both",
                "owners": ["111"],
            }
        ]
        self.plugin = self.module.PrNotify(self.context, self.config)
        with self.assertRaisesRegex(ValueError, "群号"):
            await self.plugin.initialize()
        self.assertEqual(self.plugin.store.get_repo("org/repo")["target"], "100")

    async def test_inner_reaction_result_retries_and_deduplicates(self):
        """Regression: NapCat's outer success must not hide an inner failure."""
        await self.bind()
        await self.post("pull_request", self.payload(), "open")
        original = self.bot.call_action

        async def failure(action, **kwargs):
            if action == "set_msg_emoji_like":
                return {"result": 65011, "errMsg": "secret test-secret"}
            return await original(action, **kwargs)

        with patch.object(self.bot, "call_action", failure):
            await self.post("pull_request", self.payload("closed"), "close")
            row = self.plugin.store.db.execute(
                "SELECT * FROM tasks WHERE operation='reaction'"
            ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertFalse(
                self.plugin.store.delivered("org/repo", "close", "group:100")
            )
            self.assertNotIn("test-secret", row["error"])
            for expected in (2, 3, 4):
                with self.plugin.store.db:
                    self.plugin.store.db.execute(
                        "UPDATE tasks SET next_at=0 WHERE id=?", (row["id"],)
                    )
                await asyncio.sleep(0.25)
                row = self.plugin.store.task(row["id"])
                self.assertEqual(row["attempts"], expected)
            self.assertEqual(row["status"], "failed")
        self.plugin.store.retry(row["id"])
        with patch.object(
            self.bot, "call_action", AsyncMock(return_value={"result": 65002})
        ):
            await asyncio.sleep(0.25)
        self.assertTrue(self.plugin.store.delivered("org/repo", "close", "group:100"))
        count = len(self.bot.calls)
        await self.post("pull_request", self.payload("closed"), "close")
        self.assertEqual(len(self.bot.calls), count)

    async def test_webhook_ack_and_close_wait_for_pending_open(self):
        """HTTP acceptance does not wait for QQ; a reaction uses the accepted open."""
        await self.bind()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_action

        async def slow(action, **kwargs):
            if action == "send_group_msg":
                entered.set()
                await release.wait()
            return await original(action, **kwargs)

        with patch.object(self.bot, "call_action", slow):
            self.assertEqual(
                await self.post("pull_request", self.payload(), "open"), 200
            )
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(
                await self.post("pull_request", self.payload("closed"), "close"), 200
            )
            self.assertEqual(self.bot.calls, [])
            release.set()
            await asyncio.sleep(0.5)
        self.assertEqual(
            [a for a, _ in self.bot.calls], ["send_group_msg", "set_msg_emoji_like"]
        )
        self.assertEqual(self.bot.calls[1][1]["message_id"], 1)

    async def test_timeout_is_unknown_and_manual_retry_only(self):
        """Messages with a missing acknowledgement are not automatically duplicated."""
        await self.bind()
        with patch.object(self.bot, "call_action", AsyncMock(side_effect=TimeoutError)):
            await self.post("pull_request", self.payload(), "timeout")
        row = self.plugin.store.db.execute(
            "SELECT * FROM tasks WHERE delivery='timeout'"
        ).fetchone()
        self.assertEqual(row["status"], "unknown")
        await self.post("pull_request", self.payload(), "timeout")
        self.assertEqual(self.bot.calls, [])
        self.plugin.store.retry(row["id"])
        await asyncio.sleep(0.25)
        self.assertEqual(self.plugin.store.task(row["id"])["status"], "success")

    async def test_restart_recovery_preserves_legacy_and_unknown(self):
        """An old SQLite database remains usable; interrupted messages stay unknown."""
        await self.bind()
        self.plugin.store.remember(
            "org/repo", "legacy", "group:100", (6, "group", "100", "456")
        )
        await self.plugin.dispatcher.stop()
        await self.plugin.pull_request(
            "org/repo", "interrupted", "opened", self.payload()["pull_request"]
        )
        task = self.plugin.store.claim()
        self.assertIsNotNone(task)
        self.plugin.store.recover()
        self.assertEqual(self.plugin.store.task(task["id"])["status"], "unknown")
        self.assertTrue(self.plugin.store.delivered("org/repo", "legacy", "group:100"))
        self.assertEqual(
            self.plugin.store.messages("org/repo", 6)[0]["message_id"], "456"
        )

    async def test_changed_recipients_cancel_queued_tasks(self):
        """Removing a QQ cancels both their DM and a queued group mention."""
        await self.bind()
        await self.command("/notify mode org/repo both")
        await self.plugin.dispatcher.stop()
        await self.plugin.pull_request(
            "org/repo", "stale", "opened", self.payload()["pull_request"]
        )
        await self.command("/notify owner remove org/repo 111")
        tasks = self.plugin.store.db.execute(
            "SELECT * FROM tasks WHERE delivery='stale'"
        ).fetchall()
        self.assertEqual(
            {(t["kind"], t["target"]): t["status"] for t in tasks},
            {
                ("group", "100"): "cancelled",
                ("private", "111"): "cancelled",
                ("private", "222"): "pending",
            },
        )
        await self.command("/notify repo remove org/repo")
        self.assertTrue(
            all(
                t["status"] == "cancelled"
                for t in self.plugin.store.db.execute(
                    "SELECT * FROM tasks WHERE delivery='stale'"
                )
            )
        )

    async def test_dashboard_uses_configured_targets_and_safe_status(self):
        """Authenticated page handlers expose metadata, never payloads or credentials."""
        await self.bind()
        dashboard_module = sys.modules[self.plugin.dashboard.__class__.__module__]
        fake_request = types.SimpleNamespace(
            json=AsyncMock(return_value={"repository": "org/repo", "kind": "group"})
        )
        with patch.object(dashboard_module, "request", fake_request):
            result = await self.plugin.dashboard.test()
            self.assertEqual(len(result["task_ids"]), 5)
            await asyncio.sleep(0.5)
            fake_request.json.return_value = {
                "repository": "org/repo",
                "kind": "private",
                "target": "987",
            }
            self.assertEqual((await self.plugin.dashboard.test())["status"], "error")
            fake_request.json.return_value = {"repository": "org/repo"}
            with patch.object(
                self.bot,
                "call_action",
                AsyncMock(return_value={"online": True, "good": True}),
            ):
                self.assertTrue((await self.plugin.dashboard.check())["connected"])
        status = await self.plugin.dashboard.status()
        encoded = json.dumps(status)
        self.assertNotIn("test-secret", encoded)
        self.assertNotIn("content", encoded)
        self.assertEqual(self.plugin.store.messages("org/repo", 0), [])
        self.assertEqual(
            len([a for a, _ in self.bot.calls if a == "set_msg_emoji_like"]), 2
        )

    async def test_worker_concurrency_is_bounded(self):
        """At most four independent QQ destinations are in flight."""
        await self.bind()
        await self.command("/notify mode org/repo private")
        await self.command("/notify owner add org/repo 333 444 555")
        release = asyncio.Event()
        active = peak = 0
        original = self.bot.call_action

        async def slow(action, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return await original(action, **kwargs)

        with patch.object(self.bot, "call_action", slow):
            await self.post("pull_request", self.payload())
            self.assertEqual(active, 4)
            release.set()
            await asyncio.sleep(0.5)
        self.assertEqual(peak, 4)
        self.assertEqual(len(self.bot.calls), 5)


if __name__ == "__main__":
    unittest.main()
