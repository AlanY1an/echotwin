"""All translatable strings — keyed by dotted name, mapped per Discord locale.

Locale codes follow Discord's spec (https://discord.com/developers/docs/reference#locales).
Only en-US and zh-CN provided; other zh- variants fall back to zh-CN automatically.
"""
from __future__ import annotations

from typing import Any

import discord

DEFAULT_LOCALE = "en-US"


# Locale aliases — when a user's client locale isn't directly in the table, fall
# back to a related one before giving up to en-US.
_LOCALE_ALIASES: dict[str, str] = {
    "zh-TW": "zh-CN",
    "zh-HK": "zh-CN",
}


STRINGS: dict[str, dict[str, str]] = {
    # ---------- /join /leave /say /sleep /wake /persona ----------
    "cmd.join.desc": {
        "en-US": "Join your current voice channel",
        "zh-CN": "bot 加入你当前的语音频道",
    },
    "cmd.leave.desc": {
        "en-US": "Leave the voice channel (with farewell)",
        "zh-CN": "bot 离开语音频道(带告别)",
    },
    "cmd.say.desc": {
        "en-US": "Speak the given text in the cloned voice",
        "zh-CN": "让 bot 用克隆声音念一段文字",
    },
    "cmd.say.param.text": {
        "en-US": "Text to speak (max 500 chars)",
        "zh-CN": "要念的内容(500 字内)",
    },
    "cmd.sleep.desc": {
        "en-US": "Stay in channel but go quiet (use /wake to resume)",
        "zh-CN": "bot 留频道但安静(用 /wake 唤醒)",
    },
    "cmd.wake.desc": {
        "en-US": "Wake from sleep mode",
        "zh-CN": "解除 sleep",
    },
    "cmd.persona.desc": {
        "en-US": "Persona inspection (current | list)",
        "zh-CN": "人格管理(普通用户:current|list)",
    },
    "cmd.persona.param.action": {
        "en-US": "current = show active; list = show all",
        "zh-CN": "current=看当前;list=列出所有",
    },

    # responses
    "resp.user_not_in_voice": {
        "en-US": "❌ You're not in a voice channel",
        "zh-CN": "❌ 你不在语音频道",
    },
    "resp.joined": {
        "en-US": "✅ Joined {channel}",
        "zh-CN": "✅ 已加入 {channel}",
    },
    "resp.join_failed": {
        "en-US": "❌ Join failed: {error}",
        "zh-CN": "❌ 加入失败: {error}",
    },
    "resp.bot_not_in_voice": {
        "en-US": "❌ I'm not in a voice channel",
        "zh-CN": "❌ 我不在语音频道",
    },
    "resp.leaving": {
        "en-US": "👋 Bye~",
        "zh-CN": "👋 拜拜~",
    },
    "resp.bot_not_in_voice_join_first": {
        "en-US": "❌ I'm not in a voice channel — try /join first",
        "zh-CN": "❌ 我不在语音频道,先 /join",
    },
    "resp.text_too_long": {
        "en-US": "❌ Too long — keep it under 500 characters",
        "zh-CN": "❌ 太长了,500 字以内",
    },
    "resp.spoken": {
        "en-US": "✅ Done: {snippet}…",
        "zh-CN": "✅ 念完: {snippet}…",
    },
    "resp.generic_failure": {
        "en-US": "❌ Failed: {error}",
        "zh-CN": "❌ 失败: {error}",
    },
    "resp.sleeping_now": {
        "en-US": "💤 Going quiet for a bit~",
        "zh-CN": "💤 我安静一会儿~",
    },
    "resp.waking_up": {
        "en-US": "😊 I'm back!",
        "zh-CN": "😊 我回来啦",
    },
    "resp.not_sleeping": {
        "en-US": "I'm not sleeping",
        "zh-CN": "我没在睡呢",
    },
    "resp.persona_current": {
        "en-US": "🎭 Active persona: **{id}** ({name})",
        "zh-CN": "🎭 当前人格:**{id}** ({name})",
    },

    # ---------- /persona-admin ----------
    "grp.persona_admin.desc": {
        "en-US": "Persona admin (owner only, DM context)",
        "zh-CN": "人格管理(owner only,DM 调用)",
    },
    "cmd.persona_admin.use.desc": {
        "en-US": "Switch persona (clears all guild histories)",
        "zh-CN": "切换人格(清空所有 guild 历史)",
    },
    "cmd.persona_admin.use.param.name": {
        "en-US": "Persona id (no .md extension)",
        "zh-CN": "人格名称(不带 .md)",
    },
    "cmd.persona_admin.reload.desc": {
        "en-US": "Re-read the current persona file (no restart)",
        "zh-CN": "不重启 bot,重读当前 persona 文件",
    },
    "resp.persona_unknown": {
        "en-US": "❌ Unknown persona `{name}` — available: {available}",
        "zh-CN": "❌ 未知人格 `{name}`,可用: {available}",
    },
    "resp.persona_load_failed": {
        "en-US": "❌ Failed to load persona: {error}",
        "zh-CN": "❌ 加载 persona 失败: {error}",
    },
    "resp.persona_switched": {
        "en-US": "✅ Switched to `{name}` ({display}); cleared all guild conversation history",
        "zh-CN": "✅ 切换到 `{name}` ({display}),已清空所有 guild 对话历史",
    },
    "resp.persona_reload_failed": {
        "en-US": "❌ Reload failed: {error}",
        "zh-CN": "❌ 重载失败: {error}",
    },
    "resp.persona_reloaded": {
        "en-US": "✅ Reloaded `{id}`",
        "zh-CN": "✅ 重载 `{id}` 完成",
    },

    # ---------- /voice-admin ----------
    "grp.voice_admin.desc": {
        "en-US": "Fish Audio voice management (owner only)",
        "zh-CN": "Fish Audio voice 管理(owner only)",
    },
    "cmd.voice_admin.set.desc": {
        "en-US": "Override the Fish Audio voice ID",
        "zh-CN": "切换 Fish Audio voice ID",
    },
    "cmd.voice_admin.set.param.voice_id": {
        "en-US": "Fish Audio model ID",
        "zh-CN": "Fish Audio model ID",
    },
    "cmd.voice_admin.show.desc": {
        "en-US": "Show the effective voice ID currently in use",
        "zh-CN": "显示当前 effective voice ID",
    },
    "resp.voice_set": {
        "en-US": "✅ Voice switched to `{voice_id}`",
        "zh-CN": "✅ Voice 切换到 `{voice_id}`",
    },
    "resp.voice_show_override": {
        "en-US": "🎙 effective voice: `{eff}`\n   (override; persona default: `{persona}`)",
        "zh-CN": "🎙 effective voice: `{eff}`\n   (override; persona default: `{persona}`)",
    },
    "resp.voice_show_persona": {
        "en-US": "🎙 effective voice: `{eff}`  (from persona)",
        "zh-CN": "🎙 effective voice: `{eff}`  (from persona)",
    },

    # ---------- /admin ----------
    "grp.admin.desc": {
        "en-US": "Operations admin (owner only)",
        "zh-CN": "运维管理(owner only)",
    },
    "cmd.admin.cost.desc": {
        "en-US": "Show today/this month spending",
        "zh-CN": "查看本月/今日花费",
    },
    "cmd.admin.health.desc": {
        "en-US": "Show bot internal status",
        "zh-CN": "bot 内部状态",
    },
    "cmd.admin.wakeword.desc": {
        "en-US": "Toggle wake-word required mode",
        "zh-CN": "开关 wake word 模式",
    },
    "cmd.admin.wakeword.param.state": {
        "en-US": "on = require wake word; off = always respond",
        "zh-CN": "on=必须叫名字才回应;off=随时回应",
    },
    "cmd.admin.reload_config.desc": {
        "en-US": "Hot-reload config.yaml + persona file",
        "zh-CN": "热加载 config.yaml + persona 文件",
    },
    "cmd.admin.restart.desc": {
        "en-US": "Soft-restart all sessions (process stays up)",
        "zh-CN": "软重启所有 session(不重启进程)",
    },
    "cmd.admin.whitelist.desc": {
        "en-US": "Voice whitelist management (empty = listen to everyone)",
        "zh-CN": "语音白名单管理(空名单=听所有人)",
    },
    "cmd.admin.whitelist.param.action": {
        "en-US": "add / remove / list / clear (clear = listen to all)",
        "zh-CN": "add=加白名单 / remove=移除 / list=查看 / clear=清空(听所有人)",
    },
    "cmd.admin.whitelist.param.user": {
        "en-US": "Target user (required for add/remove)",
        "zh-CN": "目标用户(add/remove 必填)",
    },
    "cmd.admin.owner.desc": {
        "en-US": "Co-owner management (primary owner only)",
        "zh-CN": "副 owner 管理 (仅 Discord app owner 可操作)",
    },
    "cmd.admin.owner.param.action": {
        "en-US": "add / remove / list",
        "zh-CN": "add=新增 / remove=移除 / list=查看",
    },
    "cmd.admin.owner.param.user": {
        "en-US": "Target user (required for add/remove)",
        "zh-CN": "目标用户 (add/remove 必填)",
    },

    # admin responses
    "resp.cost_disabled": {
        "en-US": "Cost tracking is disabled",
        "zh-CN": "成本统计未启用",
    },
    "resp.cost_header": {
        "en-US": "💰 **Cost summary**",
        "zh-CN": "💰 **成本统计**",
    },
    "resp.cost_today": {
        "en-US": "**Today**: ${total:.4f}",
        "zh-CN": "**今日**:${total:.4f}",
    },
    "resp.cost_month": {
        "en-US": "**This month**: ${total:.4f}",
        "zh-CN": "**本月**:${total:.4f}",
    },
    "resp.health": {
        "en-US": (
            "🏥 **Health**\n"
            "Uptime: {h}h{m}m\n"
            "Guilds: {guilds}\n"
            "Active sessions: {sessions}\n"
            "Persona: {persona_id} ({persona_name})\n"
            "Voice ID: `{voice_short}…`"
        ),
        "zh-CN": (
            "🏥 **健康**\n"
            "运行时间: {h}h{m}m\n"
            "Guilds: {guilds}\n"
            "活跃 sessions: {sessions}\n"
            "Persona: {persona_id} ({persona_name})\n"
            "Voice ID: `{voice_short}…`"
        ),
    },
    "resp.wakeword_set": {
        "en-US": "✅ Wake word required = {state}",
        "zh-CN": "✅ Wake word required = {state}",
    },
    "resp.config_watcher_missing": {
        "en-US": "❌ config watcher not initialized",
        "zh-CN": "❌ config watcher 未初始化",
    },
    "resp.reload_done": {
        "en-US": "✅ Reloaded\n```\n{diff}\n```",
        "zh-CN": "✅ 重载完成\n```\n{diff}\n```",
    },
    "resp.reload_done_no_changes": {
        "en-US": "✅ Reloaded — no config changes (persona refreshed if file changed)",
        "zh-CN": "✅ 重载完成,无变更(persona 已更新如果文件改了)",
    },
    "resp.sessions_reset": {
        "en-US": "✅ Reset {count} sessions",
        "zh-CN": "✅ 重置了 {count} 个 session",
    },
    "resp.whitelist_empty": {
        "en-US": "📋 Whitelist empty → bot listens to everyone (default)",
        "zh-CN": "📋 白名单为空 → bot 听所有人(默认)",
    },
    "resp.whitelist_header": {
        "en-US": "📋 Current whitelist (only listens to these users):",
        "zh-CN": "📋 当前白名单(只听这些用户):",
    },
    "resp.whitelist_cleared": {
        "en-US": "✅ Whitelist cleared → bot now listens to everyone",
        "zh-CN": "✅ 已清空白名单 → bot 现在听所有人",
    },
    "resp.action_needs_user": {
        "en-US": "❌ {action} requires the user parameter",
        "zh-CN": "❌ {action} 需要指定 user 参数",
    },
    "resp.whitelist_user_already": {
        "en-US": "ℹ️ {name} ({id}) is already on the whitelist",
        "zh-CN": "ℹ️ {name} ({id}) 已在白名单",
    },
    "resp.whitelist_added": {
        "en-US": "✅ Added to whitelist: {name} ({id})",
        "zh-CN": "✅ 加入白名单: {name} ({id})",
    },
    "resp.whitelist_user_not_present": {
        "en-US": "ℹ️ {name} is not on the whitelist",
        "zh-CN": "ℹ️ {name} 不在白名单",
    },
    "resp.whitelist_removed": {
        "en-US": "✅ Removed: {name}",
        "zh-CN": "✅ 已移除: {name}",
    },
    "resp.owner_primary_only": {
        "en-US": "❌ Only the primary owner (Discord app owner) can manage co-owners",
        "zh-CN": "❌ 仅主 owner (Discord app owner) 能管理副 owner",
    },
    "resp.owner_primary_label": {
        "en-US": "👑 Primary owner: {id} ({name})",
        "zh-CN": "👑 主 owner: {id} ({name})",
    },
    "resp.owner_co_empty": {
        "en-US": "📋 Co-owners: none",
        "zh-CN": "📋 副 owner: 无",
    },
    "resp.owner_co_header": {
        "en-US": "📋 Co-owners:",
        "zh-CN": "📋 副 owner:",
    },
    "resp.owner_cant_self_add": {
        "en-US": "❌ The primary owner already has full permissions; no need to add as co-owner",
        "zh-CN": "❌ 主 owner 自动有权限,不需要加到副 owner 列表",
    },
    "resp.owner_already_co": {
        "en-US": "ℹ️ {name} ({id}) is already a co-owner",
        "zh-CN": "ℹ️ {name} ({id}) 已是副 owner",
    },
    "resp.owner_added": {
        "en-US": "✅ Added co-owner: {name} ({id})",
        "zh-CN": "✅ 加入副 owner: {name} ({id})",
    },
    "resp.owner_not_co": {
        "en-US": "ℹ️ {name} is not a co-owner",
        "zh-CN": "ℹ️ {name} 不是副 owner",
    },
    "resp.owner_removed": {
        "en-US": "✅ Removed co-owner: {name}",
        "zh-CN": "✅ 已移除副 owner: {name}",
    },

    # ---------- choice display names ----------
    "choice.persona.current": {"en-US": "current", "zh-CN": "current"},
    "choice.persona.list": {"en-US": "list", "zh-CN": "list"},
    "choice.action.add": {"en-US": "add", "zh-CN": "add"},
    "choice.action.remove": {"en-US": "remove", "zh-CN": "remove"},
    "choice.action.list": {"en-US": "list", "zh-CN": "list"},
    "choice.action.clear": {"en-US": "clear", "zh-CN": "clear"},
    "choice.state.on": {"en-US": "on", "zh-CN": "on"},
    "choice.state.off": {"en-US": "off", "zh-CN": "off"},
}


def _normalize_locale(locale: discord.Locale | str | None) -> str:
    if locale is None:
        return DEFAULT_LOCALE
    code = str(locale)
    return _LOCALE_ALIASES.get(code, code)


def t(key: str, locale: discord.Locale | str | None = None, **fmt: Any) -> str:
    """Translate a key. Falls back to en-US, then any available locale, then key."""
    table = STRINGS.get(key)
    if table is None:
        return key
    code = _normalize_locale(locale)
    text = table.get(code) or table.get(DEFAULT_LOCALE)
    if text is None:
        text = next(iter(table.values()))
    if fmt:
        try:
            text = text.format(**fmt)
        except (KeyError, IndexError):
            pass
    return text
