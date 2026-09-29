"""Authenticated Plugin Page endpoints for delivery diagnostics and explicit tests."""

import asyncio
import secrets
import time

from astrbot.api.web import error_response, json_response, request

from .delivery import call, checked_result


class Dashboard:
    """Expose only configured destinations and safe delivery metadata to the panel."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.store = plugin.store
        self.connections = {}

    def register(self):
        for name, method in [
            ("status", "GET"),
            ("check", "POST"),
            ("test", "POST"),
            ("retry", "POST"),
        ]:
            self.plugin.context.register_web_api(
                f"/astrbot_plugin_pr_notify/{name}",
                getattr(self, name),
                [method],
                "PR 通知状态与自检",
            )

    async def status(self):
        repos = []
        for repo in self.store.repos():
            name = repo["name"]
            received = self.store.db.execute(
                "SELECT received,event FROM requests WHERE repo=?", (name,)
            ).fetchone()
            tasks = self.store.db.execute(
                """SELECT id,number,kind,target,operation,test,status,attempts,
                next_at,created,updated,duration,error,
                (SELECT MIN(p.id) FROM tasks p WHERE p.repo=t.repo AND p.number=t.number
                    AND p.kind=t.kind AND p.target=t.target AND p.id<t.id
                    AND p.status IN ('pending','running','failed','unknown')) AS blocked_by
                FROM tasks t WHERE repo=? AND
                (updated>=? OR status IN ('pending','running','failed','unknown')) ORDER BY id DESC""",
                (name, time.time() - 30 * 86400),
            ).fetchall()
            repos.append(
                {
                    "name": name,
                    "mode": repo["mode"],
                    "platform": repo["platform"],
                    "bot_qq": repo["self_id"],
                    "group_id": repo["target"] if repo["kind"] == "group" else "",
                    "owners": self.store.owners(name),
                    "connection": self.connections.get(name),
                    "received": dict(received) if received else None,
                    "tasks": [dict(t) for t in tasks],
                }
            )
        return json_response(
            {"listening": self.plugin.runner is not None, "repositories": repos}
        )

    async def check(self):
        payload = await request.json()
        repo = self.read_repo(payload, {"repository"})
        if repo is None:
            return error_response("请选择已配置的仓库")
        try:
            client = self.plugin.client(repo)
            result = checked_result(
                await call(
                    client,
                    "get_status",
                    **({"self_id": repo["self_id"]} if repo["self_id"] else {}),
                )
            )
            connected = (
                result.get("online") is True and result.get("good", True) is True
            )
            reason = "机器人已连接" if connected else "机器人离线或状态异常"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- never expose raw adapter exception payloads
            connected, reason = (
                False,
                "机器人连接检查失败，请检查平台 ID 和机器人在线状态",
            )
        valid = bool(self.store.owners(repo["name"])) and (
            repo["mode"] == "private" or repo["kind"] == "group"
        )
        result = {
            "connected": connected,
            "checked_at": time.time(),
            "reason": reason,
            "configuration_ok": valid,
            "listening": self.plugin.runner is not None,
        }
        self.connections[repo["name"]] = result
        return json_response(result)

    def read_repo(self, payload, fields):
        if (
            not isinstance(payload, dict)
            or set(payload) != fields
            or not isinstance(payload["repository"], str)
        ):
            return None
        return self.store.get_repo(payload["repository"].lower())

    async def test(self):
        payload = await request.json()
        repo = self.read_repo(payload, {"repository", "kind"})
        if repo is None or payload["kind"] not in ("group", "private"):
            return error_response("请选择已配置的仓库和通知类型")
        targets = [
            (kind, target)
            for kind, target in self.plugin.targets(repo)
            if kind == payload["kind"]
        ]
        if not targets or not self.store.owners(repo["name"]):
            return error_response("该通知类型未启用或接收者未配置")
        pending = self.store.db.execute(
            "SELECT 1 FROM tasks WHERE repo=? AND kind=? AND test=1 AND status IN ('pending','running')",
            (repo["name"], payload["kind"]),
        ).fetchone()
        if pending:
            return error_response("同类测试正在进行，请等待结果")
        token = f"test-{secrets.token_hex(12)}"
        ids = []
        with self.store.db:
            for kind, target in targets:
                checks = [("消息", None)]
                if kind == "group":
                    checks += [
                        (label, self.plugin.config[field])
                        for label, field in [
                            ("合并表情", "merged_reaction_emoji_id"),
                            ("关闭表情", "closed_reaction_emoji_id"),
                        ]
                        if self.plugin.config[field]
                    ]
                for index, (label, emoji) in enumerate(checks):
                    delivery = f"{token}-{index}"
                    parent = self.store.enqueue(
                        repo,
                        delivery,
                        0,
                        kind,
                        target,
                        "message",
                        self.plugin.segments(
                            repo, kind, f"[测试] PR 通知 · {label}\n{repo['name']}"
                        ),
                        test=True,
                    )
                    ids.append(parent)
                    if emoji:
                        ids.append(
                            self.store.enqueue(
                                repo,
                                delivery,
                                0,
                                kind,
                                target,
                                "reaction",
                                emoji,
                                test=True,
                                parent=parent,
                            )
                        )
        return json_response({"task_ids": ids})

    async def retry(self):
        payload = await request.json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"task_id"}
            or type(payload["task_id"]) is not int
        ):
            return error_response("需要已有任务 ID")
        try:
            self.store.retry(payload["task_id"])
        except ValueError as exc:
            return error_response(str(exc))
        return json_response({"queued": True})
