"""Long-lived PR plugin contracts: signed HTTP, QQ routing and persisted state.

Owned by this plugin. AstrBot/QQ are faked; aiohttp and SQLite run for real.
"""

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
from unittest.mock import patch

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
            raise RuntimeError("private delivery unavailable")
        self.calls.append((action, kwargs))
        return {"message_id": len(self.calls)}


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
            get_platform_inst=lambda name: platform if name == "qq-main" else None
        )
        modules = {}
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.star",
        ):
            modules[name] = types.ModuleType(name)
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

    async def post(self, event, payload, delivery="delivery-1", signature=None):
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature
            or "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest(),
        }
        response = await self.http.post("/github/webhook", data=body, headers=headers)
        await response.read()
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
        with self.assertLogs("test.pr-notify", level="ERROR"):
            self.assertEqual(await self.post("pull_request", self.payload()), 503)
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


if __name__ == "__main__":
    unittest.main()
