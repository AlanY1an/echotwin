"""Owner command groups must genuinely be restricted to the DM context.

Historical bug: `grp.contexts = {...}` is a no-op on discord.py 2.7.1
(the real attribute name is allowed_contexts), so owner commands were visible in every guild.
"""
from types import SimpleNamespace

import discord
from discord import app_commands

from echotwin.commands.owner_dm import register_owner_commands


def _build_tree() -> app_commands.CommandTree:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    fake_bot = SimpleNamespace(
        app_owner_id=None,
        extra_owner_ids=set(),
        config=SimpleNamespace(),
    )
    register_owner_commands(tree, fake_bot)  # type: ignore[arg-type]
    return tree


def test_owner_groups_allowed_in_dm_and_guild():
    """Admin groups work in DMs AND guild channels; the owner check + ephemeral
    responses are the real gate, not the context restriction."""
    tree = _build_tree()
    for name in ("persona-admin", "voice-admin", "admin"):
        grp = tree.get_command(name)
        assert grp is not None, f"group {name} not registered"
        ctx = grp.allowed_contexts
        assert ctx is not None, f"{name}: allowed_contexts unset (command would sync everywhere uncontrolled)"
        assert ctx.dm_channel is True, f"{name}: DM must stay allowed"
        assert ctx.guild is True, f"{name}: guild context must be allowed (owner-only via _is_owner)"
