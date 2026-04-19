import discord
from discord.ext import commands
from discord import app_commands
import os
import io
import json
import asyncio
import chat_exporter
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
TICKET_STAFF_ROLE_ID = int(os.getenv("TICKET_STAFF_ROLE_ID"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID"))
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="$", intents=intents)

# channel_id -> { opener, added_users, product, budget, claimer }
ticket_data: dict[int, dict] = {}

TICKET_DATA_FILE = "ticket_data.json"

def load_ticket_data():
    global ticket_data
    if os.path.exists(TICKET_DATA_FILE):
        with open(TICKET_DATA_FILE, "r") as f:
            raw = json.load(f)
        ticket_data = {
            int(k): {**v, "added_users": set(v.get("added_users", []))}
            for k, v in raw.items()
        }

def save_ticket_data():
    with open(TICKET_DATA_FILE, "w") as f:
        serializable = {
            str(k): {**v, "added_users": list(v.get("added_users", set()))}
            for k, v in ticket_data.items()
        }
        json.dump(serializable, f, indent=2)

# ── Helpers ────────────────────────────────────────────────────────────────────

def is_ticket_channel(channel: discord.TextChannel) -> bool:
    return channel.name.startswith("ticket-") or channel.name.startswith("closed-")

async def generate_transcript(channel: discord.TextChannel) -> discord.File | None:
    export = await chat_exporter.export(channel)
    if export is None:
        return None
    return discord.File(io.BytesIO(export.encode()), filename=f"transcript-{channel.name}.html")

# ── Close/Delete View ──────────────────────────────────────────────────────────

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.green, custom_id="ticket_reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        data = ticket_data.get(channel.id, {})
        opener_id = data.get("opener")

        if opener_id:
            opener = interaction.guild.get_member(opener_id)
            if opener:
                await channel.set_permissions(opener, view_channel=True, send_messages=True, read_message_history=True)

        for uid in data.get("added_users", set()):
            member = interaction.guild.get_member(uid)
            if member:
                await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)

        async def _try_reopen_rename():
            try:
                await channel.edit(name=channel.name.replace("closed-", ""))
            except Exception:
                pass

        asyncio.create_task(_try_reopen_rename())
        
        reopen_embed = discord.Embed(
            title="<:blue_crown:1495333511824146495> Ticket Reopened",
            description="This ticket has been reopened.",
            color=0x3498db,
        )
        reopen_embed.add_field(name="Reopened by", value=interaction.user.mention, inline=True)
        reopen_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        reopen_embed.set_footer(text=f"Reopened by {interaction.user} • {interaction.user.id}")
        await channel.send(embed=reopen_embed)
        await interaction.response.send_message("<:blue_tick:1495334689983037504> Ticket reopened.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, custom_id="ticket_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        staff_role = interaction.guild.get_role(TICKET_STAFF_ROLE_ID)

        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("<:wrong:1495334749663793213> Only staff can delete tickets.", ephemeral=True)

        await interaction.response.send_message("<:uo_delete:1495332907705696288> Deleting ticket...", ephemeral=True)

        if not channel.name.startswith("closed-"):
            await _close_ticket(channel, interaction.guild)

        ticket_data.pop(channel.id, None)
        save_ticket_data()
        await channel.delete()

# ── Close logic ────────────────────────────────────────────────────────────────

async def _close_ticket(channel: discord.TextChannel, guild: discord.Guild, vouch_embed: discord.Embed = None, vouch_view: discord.ui.View = None):
    data = ticket_data.get(channel.id, {})
    opener_id = data.get("opener")
    claimer_id = data.get("claimer")
    product = data.get("product", "N/A")

    opener = guild.get_member(opener_id) if opener_id else None
    claimer = guild.get_member(claimer_id) if claimer_id else None
    transcript_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)

    # Build transcript embed
    t_embed = discord.Embed(title="<:transcript:1495333721706987622> Transcript", color=0x3498db)
    t_embed.add_field(name="Channel", value=f"`{channel.name}`", inline=False)
    t_embed.add_field(name="Product", value=f"```{product}```", inline=False)
    t_embed.add_field(name="Buyer", value=opener.mention if opener else f"<@{opener_id}>", inline=True)
    t_embed.add_field(name="Claimed by", value=claimer.mention if claimer else "Unclaimed", inline=True)

    transcript_file = await generate_transcript(channel)
    if transcript_file and transcript_channel:
        await transcript_channel.send(embed=t_embed, file=transcript_file)

    # DM opener: transcript first, then vouch
    if opener:
        transcript_file2 = await generate_transcript(channel)
        try:
            dm_embed = discord.Embed(
                title="<:transcript:1495333721706987622> Your Ticket Transcript",
                description=f"Your ticket `{channel.name}` has been closed.",
                color=0x3498db,
            )
            dm_embed.add_field(name="Product", value=f"```{product}```", inline=False)
            dm_embed.add_field(name="Claimed by", value=claimer.mention if claimer else "Unclaimed", inline=True)
            await opener.send(embed=dm_embed, file=transcript_file2)
            if vouch_embed:
                await opener.send(embed=vouch_embed, view=vouch_view)
        except discord.Forbidden:
            pass

    async def _try_rename():
        try:
            await channel.edit(name=f"closed-{channel.name}")
        except Exception:
            pass

    asyncio.create_task(_try_rename())

    close_embed = discord.Embed(
        title="<:blue_gem_lock:1495332626767286364> Ticket Closed",
        description="This ticket has been closed. Staff can reopen or delete it below.",
        color=0x3498db,
    )
    await channel.send(embed=close_embed, view=TicketControlView())

    # Remove perms after sending close embed
    if opener:
        await channel.set_permissions(opener, overwrite=None)
    for uid in data.get("added_users", set()):
        member = guild.get_member(uid)
        if member:
            await channel.set_permissions(member, overwrite=None)

# ── Modal ──────────────────────────────────────────────────────────────────────

class PurchaseModal(discord.ui.Modal, title="Purchase Request"):
    product = discord.ui.TextInput(label="Product", placeholder="What would you like to purchase?", max_length=100)
    qty = discord.ui.TextInput(label="Quantity", placeholder="How many?", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        staff_role = guild.get_role(TICKET_STAFF_ROLE_ID)
        category = guild.get_channel(TICKET_CATEGORY_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites,
        )

        ticket_data[ticket_channel.id] = {
            "opener": interaction.user.id,
            "added_users": set(),
            "product": self.product.value,
            "claimer": None,
        }
        save_ticket_data()

        welcome_embed = discord.Embed(
            title="<:ticket_premium:1495328766170235030> Ticket Opened",
            description=f"Hey {interaction.user.mention}, thanks for reaching out!\nA staff member will be with you shortly.\n\n",
            color=0x3498db,
        )
        welcome_embed.set_thumbnail(url=bot.user.display_avatar.url)
        welcome_embed.set_footer(text=f"Ticket by {interaction.user} • {interaction.user.id}")

        details_embed = discord.Embed(title="<:product:1495325856854179922> Order Details", color=0x3498db)
        details_embed.description = (
            f"**Product**\n```{self.product.value}```\n"
            f"**Quantity**\n```{self.qty.value}```"
        )

        mentions = f"{interaction.user.mention} {staff_role.mention if staff_role else ''}"
        await ticket_channel.send(mentions, embeds=[welcome_embed, details_embed])
        await interaction.response.send_message(f"<:blue_tick:1495334689983037504> Your ticket has been created: {ticket_channel.mention}", ephemeral=True)

# ── Dropdown ───────────────────────────────────────────────────────────────────

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Purchase Now", description="Open a purchase request ticket", emoji="<:_cart:1495325346218901574>", value="purchase"),
        ]
        super().__init__(placeholder="Select an option...", min_values=1, max_values=1, options=options, custom_id="ticket_panel_dropdown")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "purchase":
            await interaction.response.send_modal(PurchaseModal())
            await interaction.message.edit(view=TicketPanelView())

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())

# ── Slash: ticket-panel ────────────────────────────────────────────────────────

@bot.tree.command(name="ticket-panel", description="Send the ticket panel embed")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="<:_cart:1495325346218901574> DR!X MARKET",
        description="Need help or want to place an order?\nSelect an option from the dropdown below to open a ticket.",
        color=0x3498db,
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_image(url="attachment://banner.png")
    embed.set_footer(text="We'll get back to you as soon as possible.")
    await interaction.response.send_message(embed=embed, view=TicketPanelView(), file=discord.File("banner.png"))

@ticket_panel.error
async def ticket_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("<:wrong:1495334749663793213> You need Administrator permissions.", ephemeral=True)

# ── Prefix: $claim ────────────────────────────────────────────────────────────

@bot.command(name="claim")
async def claim_ticket(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:wrong:1495334749663793213> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:wrong:1495334749663793213> Only ticket support staff can claim tickets.")

    data = ticket_data.setdefault(ctx.channel.id, {})
    if data.get("claimer"):
        claimer = ctx.guild.get_member(data["claimer"])
        return await ctx.send(f"<:wrong:1495334749663793213> Already claimed by {claimer.mention if claimer else 'someone'}.")

    data["claimer"] = ctx.author.id
    save_ticket_data()
    await ctx.message.delete()

    embed = discord.Embed(
        title="<:blue_crown:1495333511824146495> Ticket Claimed",
        description="This ticket has been claimed and is now being handled.",
        color=0x3498db,
    )
    embed.add_field(name="<:bluenewDiscordUser:1495325749597704295> Staff Member", value=ctx.author.mention, inline=True)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"Claimed by {ctx.author} • {ctx.author.id}")
    await ctx.channel.send(embed=embed)

# ── Prefix: $remind ───────────────────────────────────────────────────────────

@bot.command(name="remind")
async def remind(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:wrong:1495334749663793213> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:wrong:1495334749663793213> Only staff can send reminders.")

    data = ticket_data.get(ctx.channel.id, {})
    opener_id = data.get("opener")
    opener = ctx.guild.get_member(opener_id) if opener_id else None

    if not opener:
        return await ctx.send("<:wrong:1495334749663793213> Could not find the ticket opener.")

    ticket_url = f"https://discord.com/channels/{ctx.guild.id}/{ctx.channel.id}"

    embed = discord.Embed(
        title="<:blue_clock:1495335105034719374> Ticket Reminder",
        description=f"Hey {opener.mention}, you have an open ticket waiting for your response!",
        color=0x3498db,
    )
    embed.add_field(name="Ticket", value=ctx.channel.name, inline=True)
    embed.add_field(name="Staff", value=ctx.author.mention, inline=True)
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="Please respond as soon as possible.")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Go to Ticket", url=ticket_url, emoji="<:blue_clock:1495335105034719374>"))

    try:
        await opener.send(embed=embed, view=view)
        await ctx.message.delete()
        await ctx.channel.send(f"<:blue_tick:1495334689983037504> Reminder sent to {opener.mention}.", delete_after=5)
    except discord.Forbidden:
        await ctx.send("<:wrong:1495334749663793213> Could not DM the ticket opener — they may have DMs disabled.")

# ── Prefix: $add ──────────────────────────────────────────────────────────────

@bot.command(name="add")
async def add_user(ctx: commands.Context, user: discord.Member):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:wrong:1495334749663793213> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:wrong:1495334749663793213> Only staff can add users.")

    await ctx.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    ticket_data.setdefault(ctx.channel.id, {}).setdefault("added_users", set()).add(user.id)
    save_ticket_data()
    await ctx.send(f"<:blue_tick:1495334689983037504> {user.mention} has been added to the ticket.")

# ── Prefix: $remove ───────────────────────────────────────────────────────────

@bot.command(name="remove")
async def remove_user(ctx: commands.Context, user: discord.Member):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:wrong:1495334749663793213> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:wrong:1495334749663793213> Only staff can remove users.")

    await ctx.channel.set_permissions(user, overwrite=None)
    ticket_data.get(ctx.channel.id, {}).get("added_users", set()).discard(user.id)
    save_ticket_data()
    await ctx.send(f"<:blue_tick:1495334689983037504> {user.mention} has been removed from the ticket.")

# ── Prefix: $close ────────────────────────────────────────────────────────────

@bot.command(name="close")
async def close_ticket(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:wrong:1495334749663793213> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:wrong:1495334749663793213> Only staff can close tickets.")
    if ctx.channel.name.startswith("closed-"):
        return await ctx.send("<:wrong:1495334749663793213> This ticket is already closed.")

    await ctx.message.delete()
    await _close_ticket(ctx.channel, ctx.guild)

# ── Prefix: $close.v ──────────────────────────────────────────────────────────

@bot.command(name="close.v")
async def close_vouch(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:wrong:1495334749663793213> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:wrong:1495334749663793213> Only staff can close tickets.")
    if ctx.channel.name.startswith("closed-"):
        return await ctx.send("<:wrong:1495334749663793213> This ticket is already closed.")

    data = ticket_data.get(ctx.channel.id, {})
    opener_id = data.get("opener")
    opener = ctx.guild.get_member(opener_id) if opener_id else None

    vouch_embed = discord.Embed(
        title="<:blue_crown:1495333511824146495>  DR!X MARKET VOUCH !",
        description=(
            f"Thank you {opener.mention if opener else 'valued customer'} for your purchase in our server <:heart:1495338641508270110>\n\n"
            f"Your support helps **{ctx.guild.name}** grow & we would be pleased to serve you again "
            f"& make sure to recommend our services to your friends too"
        ),
        color=0x00ff62,
    )
    vouch_embed.set_thumbnail(url=bot.user.display_avatar.url)
    vouch_embed.set_footer(text=f"Lots of <3 from {ctx.guild.name} !!")
    vouch_embed.timestamp = discord.utils.utcnow()

    vouch_view = discord.ui.View()
    vouch_view.add_item(discord.ui.Button(
        label="Vouch Here",
        style=discord.ButtonStyle.link,
        emoji="<:trust:1495338042364399666>",
        url="https://discordapp.com/channels/1480754399075635292/1488913327575797911",
    ))

    content = f"{opener.mention}"
    await ctx.channel.send(content=content, embed=vouch_embed, view=vouch_view)

    await ctx.message.delete()
    await _close_ticket(ctx.channel, ctx.guild, vouch_embed=vouch_embed, vouch_view=vouch_view)

# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    load_ticket_data()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())
    await bot.tree.sync()
    print(f"Logged in as {bot.user} | Synced slash commands")

bot.run(TOKEN)
