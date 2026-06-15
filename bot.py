"""
FeintTrade — Discord Bot
Listens in DISCORD_MINDHUB_CHANNEL_ID for !commands and shells out to discord_commands.py.
Run: python bot.py
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

TOKEN      = os.getenv("DISCORD_BOT_TOKEN")
# Bot listens in the primary command-post channel (ft-command-post); falls back to
# the legacy channel id if command_post isn't configured.
CHANNEL_ID = int(os.getenv("DISCORD_CH_COMMAND_POST") or os.getenv("DISCORD_MINDHUB_CHANNEL_ID", "0"))

COMMANDS = {"!status", "!positions", "!strategies", "!orders", "!price", "!buy", "!sell",
            "!report", "!kill", "!resume", "!cancel", "!journal", "!heartbeat", "!help",
            "!channels", "!test", "!summary", "!digest", "!research", "!benchmark",
            "!ask", "!explain", "!usage", "!cost", "!tests", "!intel"}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def run_command(command: str, args=None) -> str:
    """Blocking subprocess call — ALWAYS invoked via run_in_executor so the Discord
    event loop (and gateway heartbeat) never stalls. Synchronous calls here were why
    the bot appeared to 'stop responding' under load."""
    result = subprocess.run(
        ["python", str(ROOT / "scripts" / "discord_commands.py"), command, *(args or [])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        timeout=90,
    )
    output = result.stdout.strip() or result.stderr.strip()
    return output[:2000] if output else f"No output from `{command}`."


def _run_cycle_blocking() -> str:
    """Blocking call to orchestrator.py cycle — runs in thread pool."""
    result = subprocess.run(
        ["python", str(ROOT / "scripts" / "orchestrator.py"), "cycle"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        timeout=360,  # 6-minute ceiling
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    if not out and err:
        return f"⚠️ Cycle produced no output.\nStderr: {err[:500]}"
    return out[:3900] if out else "Cycle complete — no summary output."


async def _run_cycle_async(channel: discord.TextChannel):
    """Run the trading cycle in a thread pool, send result to channel."""
    loop = asyncio.get_event_loop()
    try:
        summary = await loop.run_in_executor(None, _run_cycle_blocking)
    except subprocess.TimeoutExpired:
        summary = "❌ Cycle timed out after 6 minutes."
    except Exception as e:
        summary = f"❌ Cycle error: {e}"

    embed = discord.Embed(
        title="⚡ Manual Cycle Complete",
        description=summary,
        color=0x00d4aa,
    )
    embed.set_footer(text="orchestrator.py cycle — fresh data + live decisions")
    await channel.send(embed=embed)


@client.event
async def on_ready():
    print(f"FeintTrade Bot online as {client.user} — command channel {CHANNEL_ID}")
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        # The channel cache can be empty on the very first connect; fetch via REST so
        # the online banner reliably lands in ft-command-post (not silently skipped).
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except Exception as e:
            print(f"Could not resolve command channel {CHANNEL_ID}: {e}")
    if channel:
        await channel.send(
            embed=discord.Embed(
                title="🤖 FeintTrade Bot Online",
                description="Ready to receive commands.\n\n"
                            "`!status` `!positions` `!strategies` `!price` `!orders` `!buy` `!sell` "
                            "`!report` `!kill` `!resume` `!cancel` `!journal` `!heartbeat`\n"
                            "`!channels` `!test` `!summary` `!digest` `!research` `!benchmark` `!help`",
                color=0x00d4aa,
            )
        )


@client.event
async def on_message(message: discord.Message):
    # Ignore own messages
    if message.author == client.user:
        return

    # Only respond in the designated channel
    if message.channel.id != CHANNEL_ID:
        return

    # Extract command — support bare !cmd or @mention !cmd
    content = message.content.strip()
    # Strip mention if present
    if client.user in message.mentions:
        content = content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

    parts = content.split()
    command = parts[0].lower() if parts else ""
    args = parts[1:]

    if command not in COMMANDS:
        if content.startswith("!"):
            await message.reply(f"Unknown command `{command}`. Use `!help` to see all commands.")
        return

    # !heartbeat — full research + decision cycle, runs async (2-3 min)
    if command == "!heartbeat":
        await message.reply(
            "🔄 **Running manual trading cycle...** (~2-3 min)\n"
            "Gathering fresh market data, checking positions, making decisions. "
            "Results will appear here when done."
        )
        asyncio.create_task(_run_cycle_async(message.channel))
        return

    # All other commands: run the subprocess in a thread pool so the gateway never blocks.
    loop = asyncio.get_event_loop()
    async with message.channel.typing():
        try:
            response = await loop.run_in_executor(None, run_command, command, args)
        except subprocess.TimeoutExpired:
            response = f"⏱️ `{command}` timed out."
        except Exception as e:
            response = f"❌ `{command}` error: {e}"

    if command in ("!status", "!positions", "!strategies", "!orders",
                   "!channels", "!summary", "!digest", "!research", "!benchmark"):
        embed = discord.Embed(description=response[:4000], color=0x2ecc71)
        await message.reply(embed=embed)
    else:
        await message.reply(response)


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)
    # Single-instance lock: bind a fixed localhost port so a stale bot left over from a
    # migration/restart can't run concurrently on the same token (two bots fight over the
    # gateway and one ends up on the wrong channel). run_bot.bat kills the prior PID first.
    import socket
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock.bind(("127.0.0.1", 49517))
        _lock.listen(1)
    except OSError:
        print("Another FeintTrade bot instance is already running — exiting.")
        sys.exit(0)
    try:
        (ROOT / "bot.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    client.run(TOKEN)
