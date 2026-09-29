# GitHub PR 通知 · AstrBot 插件

参考 [byrdocs-bot 的 PR 通知设计](https://github.com/byrdocs/byrdocs-bot/tree/main/src/plugins/pr-notify)，为 AstrBot 提供：GitHub ping 绑定仓库；PR 新建或重开时通知 code owners；合并或关闭时给原通知贴表情。新增仅群聊、仅私聊、群聊和私聊三种模式。

## 安装

需要 AstrBot 4.28.1+（4.x）及 OneBot v11 / NapCat。表情回应使用 NapCat 的 `set_msg_emoji_like` 扩展；其他 OneBot 实现需要支持该接口。

1. 在 AstrBot 插件管理页选择“通过链接安装”，填写 `https://github.com/Shxiao101/astrbot_plugin_pr_notify`。也可下载仓库 ZIP 后上传安装。
2. 在 AstrBot 的 Python 环境安装 `requirements.txt` 中的依赖，然后重载插件。
3. 插件首次加载会自动生成并保存 `webhook_secret`，在面板中点击显示并复制到 GitHub。已有 Secret 会保留，重载不会更换。
4. 将公网 HTTPS 地址的 `/github/webhook` 反向代理到 AstrBot 主机的 `6196` 端口。Docker 需映射此端口；它与 AstrBot WebUI 端口不同。
5. 在 AstrBot 配置中把操作人的 QQ 号设为机器人管理员。单纯的 QQ 群管理员身份不够。

示例 Nginx 配置（与 AstrBot 在同一主机时）：

```nginx
location = /github/webhook {
    proxy_pass http://127.0.0.1:6196;
}
```

## 使用

最少操作：添加仓库名和接收者 QQ → 保存并重载 → 在 GitHub 添加 Webhook，填写回调地址并粘贴面板自动生成的 Secret。默认私聊；群通知需另填群号。

### 在插件面板配置（推荐）

打开 **插件 → GitHub PR 通知 → 配置 → 仓库通知配置**，添加一条“GitHub 仓库”，填写：

- 仓库名：`owner/repo`。
- AstrBot 平台 ID：机器人页面中的 ID，例如 `default`；请以实际配置为准。
- 接收通知的 QQ 号列表：每项一个 QQ 号，即 code owners。
- 通知模式：`private`、`group` 或 `both`。群通知需要填写群号。
- 机器人自身 QQ：通常留空；同一适配器连接多个 QQ 时指定发送账号。

保存并重载插件后生效，不需要再执行 `/notify repo add` 或等待 ping。GitHub Webhook 仍需按下表设置，Secret 从插件面板复制。

面板管理的仓库以面板为准，聊天命令不会修改它。禁用或删除面板条目会移除该仓库监听及记录；其他通过聊天命令添加的仓库保留。修改机器人或目标群会清除旧消息关联，避免给错误账号的消息贴表情。

### 使用聊天命令配置

在需要接收通知的群中发送：

```text
/notify repo add owner/repo
```

然后到 GitHub 仓库 **Settings → Webhooks → Add webhook** 设置：

| 字段 | 值 |
| --- | --- |
| Payload URL | `https://你的域名/github/webhook` |
| Content type | `application/json` |
| Secret | 与插件的 `webhook_secret` 一致 |
| Events | Let me select individual events → Pull requests |

5 分钟内到达的匹配仓库 ping 会完成绑定并回复确认。已有 Webhook 可在 GitHub Recent Deliveries 中重新投递 ping。`/notify repo add` 不指定仓库时，绑定下一个通过签名验证的仓库 ping，与原 bot 行为一致；建议指定仓库名。

绑定后配置接收者：

```text
/notify owner add owner/repo @用户
/notify owner add owner/repo 123456789 987654321
/notify mode owner/repo both
```

模式含义：

| 模式 | PR 新建或重开时 |
| --- | --- |
| `group` | 在绑定群中 @ 所有 code owners（默认） |
| `private` | 分别私聊所有 code owners |
| `both` | 群内 @，同时分别私聊 code owners |

私聊接收者是配置的 code owners，需要能收到机器人私聊消息（通常需先加机器人为好友）。插件不自动添加好友。一个接收者发送失败，不影响其他接收者。

也可直接在与机器人的私聊中执行 `/notify repo add owner/repo`，再添加 code owners；这类绑定固定使用 `private`，不会把管理员自动加入接收者。没有 code owners 时不发送 PR 通知，与原 bot 一致。

完整命令：

```text
/notify
/notify repo add [owner/repo]
/notify repo list
/notify repo remove <owner/repo>
/notify owner add <owner/repo> @用户或QQ号（可多个）
/notify owner remove <owner/repo> @用户或QQ号（可多个）
/notify owner list [owner/repo]
/notify mode <owner/repo> <group|private|both>
```

每个仓库绑定一个会话，命令只管理当前机器人、当前会话下的仓库。更换群或绑定机器人时，先在原会话移除，再重新绑定并添加 owners。命令前缀 `/` 以 AstrBot 的唤醒配置为准。

## 配置与状态

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `webhook_public_url` | 空 | 公网完整回调地址，可从面板复制；用于绑定提示，需先配置对应反向代理 |
| `webhook_host` | `0.0.0.0` | HTTP 监听地址 |
| `webhook_port` | `6196` | HTTP 监听端口 |
| `webhook_secret` | 自动生成 | 从面板复制到 GitHub；清空后重载会重新生成 |
| `notification_mode` | `group` | 新绑定群仓库的初始模式；已有仓库用命令修改 |
| `merged_reaction_emoji_id` | 空 | 合并后的 NapCat emoji ID；空表示禁用 |
| `closed_reaction_emoji_id` | 空 | 未合并关闭后的 NapCat emoji ID；空表示禁用 |

配置修改后重载插件。表情 ID 使用 NapCat 接受的 ID，而非直接输入 emoji 字符。关闭/合并时仅对群聊原通知贴表情；私聊只发送 PR 新建/重开通知。重开会发新通知，下次关闭关联最新群通知。

SQLite 状态保存在 `data/plugin_data/astrbot_plugin_pr_notify/notify.sqlite`，重载/重启后保留仓库、owners、原消息 ID 和已成功投递记录。等待 ping 的绑定状态在重启后失效。移除仓库会一并删除这些记录。

### 通知状态页（v1.3.0）

打开 **插件 → GitHub PR 通知 → 插件页面 → 通知状态**。仓库配置继续使用原配置面板；此页显示当前生效配置、最近一次通过签名校验的 GitHub 请求、每个目标的发送结果、耗时及重试时间。

- **检查配置**：检查本地配置、监听服务和机器人连接，不发送消息，也不代表公网可达。首次升级后，需要收到新的真实 GitHub 请求，才会显示 GitHub 已连通。
- **测试群聊与表情**：向配置群发送带 `[测试]` 标记的消息；如果配置了合并/关闭表情，各用一条测试消息验证。**测试私聊**：给配置接收者分别发送测试消息。测试不会写入真实 PR 的消息关联。
- **重试失败项**：仅重试所选失败目标。对于“结果未知”，页面会提醒可能重复送达，再由管理员决定是否重试。

Webhook 处理 `ping` 及 `pull_request` 的 `opened`、`reopened`、`closed`。任务持久化成功后立即返回 HTTP 200，此时代表**已接收**；QQ 是否送达以状态页为准。落库失败返回 HTTP 503，可在 GitHub Recent Deliveries 中 Redeliver。同一 delivery ID 已成功发送的目标会跳过。

后台最多并发 4 个目标，同仓库、同 PR、同目标按接收顺序处理。单次调用最多 15 秒；明确失败后分别等待 5、30、120 秒，总计最多尝试 4 次。自动重试次数耗尽后保留失败状态。前序任务失败或结果未知时，同一 PR 同一目标的后续通知等待管理员处理。

普通消息超时、连接异常或发送中进程退出时，标记“结果未知”，不会自动重发；人工重试仍可能产生重复消息。贴表情操作可安全重试；NapCat 内部返回失败不会记为成功，明确的“已经设置过该表情”视为完成。

重载恢复待处理任务。禁用仓库、切换目标/机器人、移除接收者时取消受影响的未完成任务；已经发给 QQ 的请求不能撤销。诊断信息保留 30 天，未解决任务及其必要依赖继续保留以支持人工处理；历史成功去重记录与原消息 ID 沿用原有生命周期。

状态页使用 AstrBot Dashboard 认证，不新增公网管理端口，也不展示 Secret、认证信息及原始 Webhook payload。当前版本保持原有 PR 事件范围；私聊不贴表情，也不发送关闭/合并通知。

## 验证

在插件目录执行：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用真实 aiohttp HTTP 请求和 SQLite；模拟 AstrBot 与 QQ 接口，覆盖签名拒绝、权限与会话隔离、ping 超时、@ 用户解析、三种模式、失败重投、重启恢复、重开后的表情关联及仓库移除。部署后仍需用真实 GitHub Webhook 和 NapCat 验证网络可达、私聊权限与表情支持。

接口参考：[AstrBot 插件开发](https://docs.astrbot.app/dev/star/plugin-new.html)、[消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)、[OneBot 适配器源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)。

## 许可证

[MIT License](LICENSE)。
