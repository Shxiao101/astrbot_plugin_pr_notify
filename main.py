import asyncio
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time

from aiohttp import web
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools

from .dashboard import Dashboard
from .delivery import Dispatcher
from .store import Store

MODES = {"group", "private", "both"}
HELP = """PR 通知管理（AstrBot 管理员）：
/notify repo add [owner/repo] — 等待 GitHub ping，5 分钟有效
/notify repo list
/notify repo remove <owner/repo>
/notify owner add <owner/repo> @用户或QQ号（可多个）
/notify owner remove <owner/repo> @用户或QQ号（可多个）
/notify owner list [owner/repo]
/notify mode <owner/repo> <group|private|both>
group：群内 @ owners；private：私聊 owners；both：两者。
在私聊中绑定仓库时默认 private，仍需添加 code owners。"""


class PrNotify(Star):
    """Bind GitHub webhooks to QQ conversations and notify repository owners."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        if config["notification_mode"] not in MODES:
            raise ValueError("notification_mode 必须为 group、private 或 both")
        self.store = Store(
            StarTools.get_data_dir("astrbot_plugin_pr_notify") / "notify.sqlite"
        )
        self.pending = None
        self.runner = None
        self.repository_secrets = {}
        self.lock = asyncio.Lock()
        self.dispatcher = Dispatcher(self)
        self.dashboard = Dashboard(self)

    async def initialize(self):
        entries = []
        names = set()
        repository_secrets = {}
        for item in self.config["repositories"]:
            if not item["enabled"]:
                continue
            name = item["repository"].strip().lower()
            platform = item["platform_id"].strip()
            self_id = item["bot_qq"].strip()
            group = item["group_id"].strip()
            mode = item["mode"]
            owners = sorted({str(qq).strip() for qq in item["owners"]})
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", name, re.ASCII) or name in names:
                raise ValueError(f"面板仓库名无效或重复：{name}，请填写 owner/repo")
            if not platform or mode not in MODES:
                raise ValueError(f"{name}：请填写平台 ID 并选择有效通知模式")
            if not owners or any(not re.fullmatch(r"[1-9][0-9]*", qq) for qq in owners):
                raise ValueError(f"{name}：接收者 QQ 列表不能为空，且须为数字")
            if (self_id and not re.fullmatch(r"[1-9][0-9]*", self_id)) or (
                group and not re.fullmatch(r"[1-9][0-9]*", group)
            ):
                raise ValueError(f"{name}：机器人 QQ 和群号须为数字")
            if mode in ("group", "both") and not group:
                raise ValueError(f"{name}：群通知需要填写群号")
            secret = item.get("webhook_secret", "")
            if not isinstance(secret, str):
                raise TypeError(f"{name}：Webhook Secret 必须为字符串")
            if secret:
                repository_secrets[name] = secret
            names.add(name)
            entries.append(
                {
                    "name": name,
                    "route": (
                        platform,
                        self_id,
                        "group" if group else "private",
                        group or owners[0],
                    ),
                    "mode": mode,
                    "owners": owners,
                }
            )
        self.store.sync_panel(entries)
        self.repository_secrets = repository_secrets
        if not self.config["webhook_secret"]:
            self.config["webhook_secret"] = secrets.token_urlsafe(32)
            self.config.save_config()
            logger.info(
                "[pr-notify] Generated webhook secret; copy it from the plugin settings"
            )
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/github/webhook", self.webhook)
        runner = web.AppRunner(app, shutdown_timeout=10)
        try:
            await runner.setup()
            await web.TCPSite(
                runner, self.config["webhook_host"], self.config["webhook_port"]
            ).start()
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        self.dashboard.register()
        self.dispatcher.start()
        logger.info(
            f"[pr-notify] Listening on port {self.config['webhook_port']}, path /github/webhook"
        )

    @filter.command("notify")
    async def notify(self, event: AstrMessageEvent):
        """管理 GitHub PR 通知仓库、code owners 与通知模式。"""
        event.stop_event()
        if not event.is_admin():
            yield event.plain_result("仅 AstrBot 管理员可以管理 PR 通知")
            return
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("此插件需要 OneBot v11（aiocqhttp / NapCat）")
            return
        # OneBot's display string may embed nicknames for @ mentions.
        # Parse only original text; obtain mention IDs from the components.
        args = " ".join(
            c.text for c in event.get_messages() if isinstance(c, Plain)
        ).split()[1:]
        route = (
            event.get_platform_id(),
            str(event.get_self_id()),
            "group" if event.get_group_id() else "private",
            str(event.get_group_id() or event.get_sender_id()),
        )
        async with self.lock:
            result = self.command(args, event, route)
            self.store.cancel_stale()
        yield event.plain_result(result)

    def command(self, args, event, route):
        if args[:2] == ["repo", "add"] and len(args) in (2, 3):
            if self.runner is None:
                return "请先配置 webhook_secret 并重载插件，确认 Webhook 启动成功"
            expected = args[2].lower() if len(args) == 3 else None
            if expected and self.store.panel_managed(expected):
                return "该仓库由插件面板管理，请在面板修改配置并重载"
            if expected and not re.fullmatch(r"[\w.-]+/[\w.-]+", expected, re.ASCII):
                return "仓库名应为 owner/repo"
            if (
                self.pending
                and self.pending["expires"] > time.monotonic()
                and self.pending["route"] != route
            ):
                return "另一个会话正在绑定仓库，请等待其完成或 5 分钟超时"
            self.pending = {
                "route": route,
                "expected": expected,
                "expires": time.monotonic() + 300,
            }
            return (
                "已进入仓库添加模式（5 分钟有效）。\n"
                "GitHub Settings > Webhooks > Add webhook：\n"
                f"Payload URL: {self.config['webhook_public_url'] or 'https://你的域名/github/webhook'}\n"
                f"反向代理至插件端口 {self.config['webhook_port']}\n"
                "Content type: application/json\nSecret: 插件配置中的 webhook_secret\n"
                "Events: Pull requests\n保存后自动用 ping 确认；已有 webhook 可 Redeliver ping。"
            )
        repos = [r for r in self.store.repos() if self.same_route(r, route)]
        if args == ["repo", "list"]:
            return (
                "\n".join(
                    f"{r['name']} [{r['mode']}] ({len(self.store.owners(r['name']))} code owners)"
                    for r in repos
                )
                or "当前会话未监听任何仓库"
            )
        if args == ["owner", "list"]:
            return (
                "\n".join(
                    f"{r['name']}：{', '.join(self.store.owners(r['name'])) or '无'}"
                    for r in repos
                )
                or "当前会话未监听任何仓库"
            )
        if len(args) < 3:
            return HELP
        name = (args[1] if args[0] == "mode" else args[2]).lower()
        if self.store.panel_managed(name):
            return "该仓库由插件面板管理，请在面板修改配置并重载"
        repo = self.store.get_repo(name)
        if repo is None or not self.same_route(repo, route):
            return f"当前会话未监听仓库 {name}"
        if args[:2] == ["repo", "remove"] and len(args) == 3:
            self.store.remove_repo(name)
            return f"已移除仓库 {name}"
        if args[0] == "mode" and len(args) == 3:
            mode = args[2]
            if mode not in MODES:
                return "模式必须为 group、private 或 both"
            if repo["kind"] == "private" and mode != "private":
                return "私聊绑定的仓库仅支持 private；请在群内重新绑定以启用群通知"
            self.store.set_mode(name, mode)
            return f"{name} 通知模式已设为 {mode}"
        if args[:2] == ["owner", "list"] and len(args) == 3:
            return (
                f"{name} 的 code owners：{', '.join(self.store.owners(name)) or '无'}"
            )
        if args[0] == "owner" and args[1] in ("add", "remove"):
            users = {
                str(c.qq)
                for c in event.get_messages()
                if isinstance(c, At) and str(c.qq) != str(event.get_self_id())
            }
            users.update(args[3:])
            if not users or any(not re.fullmatch(r"[1-9][0-9]*", u) for u in users):
                return "请 @目标用户或输入 QQ 号（可多个，用空格分隔）"
            self.store.change_owners(name, sorted(users), args[1] == "add")
            return f"已{'添加' if args[1] == 'add' else '移除'} {name} 的 code owners：{', '.join(sorted(users))}"
        return HELP

    @staticmethod
    def same_route(repo, route):
        return (
            tuple(repo[k] for k in ("platform", "self_id", "kind", "target")) == route
        )

    def client(self, repo):
        platform = self.context.get_platform_inst(repo["platform"])
        if platform is None or platform.meta().name != "aiocqhttp":
            raise RuntimeError(f"OneBot 平台不可用：{repo['platform']}")
        return platform.get_client()

    async def webhook(self, request):
        body = await request.read()
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            name = payload["repository"]["full_name"]
            if not isinstance(name, str) or not re.fullmatch(
                r"[\w.-]+/[\w.-]+", name, re.ASCII
            ):
                raise ValueError("invalid repository")
            name = name.lower()
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="invalid GitHub payload") from None
        # The repository only selects a key; trust the payload after verifying all bytes.
        secret = self.repository_secrets.get(name, self.config["webhook_secret"])
        expected = (
            "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not secret or not hmac.compare_digest(signature.encode(), expected.encode()):
            raise web.HTTPUnauthorized(text="invalid signature")
        try:
            event = request.headers.get("X-GitHub-Event", "")
            if event not in ("ping", "pull_request"):
                with self.store.db:
                    self.store.received(name, event)
                return web.json_response({"status": "ignored"})
            if event == "pull_request":
                action = payload["action"]
                if action not in ("opened", "reopened", "closed"):
                    with self.store.db:
                        self.store.received(name, "pull_request.ignored")
                    return web.json_response({"status": "ignored"})
                pr = payload["pull_request"]
                if type(pr["number"]) is not int or pr["number"] <= 0:
                    raise ValueError("invalid PR number")
                if action == "closed":
                    if type(pr["merged"]) is not bool:
                        raise ValueError("invalid merged state")
                elif not all(
                    isinstance(v, str)
                    for v in (pr["title"], pr["html_url"], pr["user"]["login"])
                ):
                    raise ValueError("invalid PR details")
            delivery = request.headers.get("X-GitHub-Delivery", "")
            if not delivery:
                raise ValueError("missing delivery ID")
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="invalid GitHub payload") from None
        except sqlite3.Error:
            raise web.HTTPServiceUnavailable(text="storage unavailable") from None
        async with self.lock:
            try:
                if event == "ping":
                    await self.ping(name)
                else:
                    await self.pull_request(name, delivery, action, pr)
                with self.store.db:
                    self.store.received(
                        name, event if event == "ping" else f"pull_request.{action}"
                    )
            except Exception:  # noqa: BLE001 -- HTTP boundary must report durable acceptance failure
                logger.exception(
                    f"[pr-notify] Webhook failed: {name}, delivery={delivery}"
                )
                raise web.HTTPServiceUnavailable(
                    text="delivery failed; redeliver from GitHub"
                ) from None
        return web.json_response({"status": "ok"})

    async def ping(self, name):
        if self.store.panel_managed(name):
            return
        pending = self.pending
        if pending is None or pending["expires"] <= time.monotonic():
            self.pending = None
            return
        if pending["expected"] and pending["expected"] != name:
            return
        repo = self.store.get_repo(name)
        if repo is not None and not self.same_route(repo, pending["route"]):
            raise ValueError("仓库已绑定其他会话，需先在原会话移除")
        if repo is None:
            mode = (
                self.config["notification_mode"]
                if pending["route"][2] == "group"
                else "private"
            )
            self.store.add_repo(name, pending["route"], mode)
            repo = self.store.get_repo(name)
        with self.store.db:
            self.store.enqueue(
                repo,
                f"ping-{secrets.token_hex(8)}",
                0,
                repo["kind"],
                repo["target"],
                "ping",
                [
                    {
                        "type": "text",
                        "data": {
                            "text": f"Webhook 连接成功！\n仓库：{name}\n请配置通知负责人 QQ。"
                        },
                    }
                ],
            )
        self.pending = None

    def targets(self, repo):
        targets = []
        if repo["mode"] in ("group", "both"):
            targets.append(("group", repo["target"]))
        if repo["mode"] in ("private", "both"):
            targets.extend(("private", qq) for qq in self.store.owners(repo["name"]))
        return targets

    def segments(self, repo, kind, text):
        segments = []
        if kind == "group":
            for qq in self.store.owners(repo["name"]):
                segments.extend(
                    [
                        {"type": "at", "data": {"qq": qq}},
                        {"type": "text", "data": {"text": " "}},
                    ]
                )
        segments.append({"type": "text", "data": {"text": text}})
        return segments

    async def pull_request(self, name, delivery, action, pr):
        repo = self.store.get_repo(name)
        if repo is None:
            return
        with self.store.db:
            if action == "closed":
                emoji = self.config[
                    "merged_reaction_emoji_id"
                    if pr["merged"]
                    else "closed_reaction_emoji_id"
                ]
                if not emoji or repo["mode"] not in ("group", "both"):
                    return
                # Capture the latest open task at receipt, not a later reopened notification.
                parent = self.store.db.execute(
                    """SELECT id FROM tasks WHERE repo=? AND number=?
                    AND kind='group' AND target=? AND operation='message' AND test=0
                    AND status!='cancelled' ORDER BY id DESC LIMIT 1""",
                    (name, pr["number"], repo["target"]),
                ).fetchone()
                old = next(
                    (
                        m
                        for m in self.store.messages(name, pr["number"])
                        if m["kind"] == "group" and m["target"] == repo["target"]
                    ),
                    None,
                )
                if (
                    parent
                    and self.store.task(parent[0])["status"] == "success"
                    and old is None
                ):
                    parent = None
                if parent is not None or old is not None:
                    self.store.enqueue(
                        repo,
                        delivery,
                        pr["number"],
                        "group",
                        repo["target"],
                        "reaction",
                        emoji,
                        parent=parent[0] if parent else None,
                        message_id=old["message_id"] if old else None,
                    )
            elif self.store.owners(name):
                text = f"[PR #{pr['number']}] {name}\n{pr['title']}\n作者：{pr['user']['login']}\n{pr['html_url']}"
                for kind, target in self.targets(repo):
                    self.store.enqueue(
                        repo,
                        delivery,
                        pr["number"],
                        kind,
                        target,
                        "message",
                        self.segments(repo, kind, text),
                    )

    async def terminate(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        await self.dispatcher.stop()
        self.pending = None
        self.store.close()
