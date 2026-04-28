import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import io
import json
import asyncio
import aiohttp
import chat_exporter
from flask import Flask
from threading import Thread
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
TICKET_STAFF_ROLE_ID = int(os.getenv("TICKET_STAFF_ROLE_ID"))
ROBUX_CATEGORY_ID = int(os.getenv("ROBUX_CATEGORY_ID"))
OTHER_CATEGORY_ID = int(os.getenv("OTHER_CATEGORY_ID"))
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID"))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "http://localhost:8080")

# ── Keep-alive server ──────────────────────────────────────────────────────────

_flask_app = Flask(__name__)

@_flask_app.route('/')
def _home():
    return "Bot is alive!"

Thread(target=lambda: _flask_app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080))), daemon=True).start()

# ── Bot setup ──────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

async def get_prefix(bot, message):
    staff_role_id = TICKET_STAFF_ROLE_ID
    if message.guild and any(r.id == staff_role_id for r in getattr(message.author, 'roles', [])):
        return ["$", ""]
    return "$"

bot = commands.Bot(command_prefix=get_prefix, intents=intents)

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
            title="<:samx_crown:1497645120848396519> Ticket Reopened",
            description="This ticket has been reopened.",
            color=0x3498db,
        )
        reopen_embed.add_field(name="Reopened by", value=interaction.user.mention, inline=True)
        reopen_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        reopen_embed.set_footer(text=f"Reopened by {interaction.user} • {interaction.user.id}")
        await channel.send(embed=reopen_embed)
        await interaction.response.send_message("<:samx_tick:1497645191463440605> Ticket reopened.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, custom_id="ticket_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        staff_role = interaction.guild.get_role(TICKET_STAFF_ROLE_ID)

        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("<:samx_wrong:1497645159037276171> Only staff can delete tickets.", ephemeral=True)

        await interaction.response.send_message("<:samx_delete:1497645076384583811> Deleting ticket...", ephemeral=True)

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
    t_embed = discord.Embed(title="<:samx_transcript:1497645146043318424> Transcript", color=0x3498db)
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
                title="<:samx_transcript:1497645146043318424> Your Ticket Transcript",
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
        title="<:samx_blue_gem_lock:1497645047951397085> Ticket Closed",
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

    # Auto-delete after 10 seconds
    await asyncio.sleep(5)
    ticket_data.pop(channel.id, None)
    save_ticket_data()
    try:
        await channel.delete()
    except Exception:
        pass

# ── Ticket action buttons (in welcome embed) ───────────────────────────────────

class TicketActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", emoji="<:samx_blue_gem_lock:1497645047951397085>", style=discord.ButtonStyle.red, custom_id="ticket_action_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        staff_role = interaction.guild.get_role(TICKET_STAFF_ROLE_ID)
        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("<:samx_wrong:1497645159037276171> Only staff can close tickets.", ephemeral=True)
        if interaction.channel.name.startswith("closed-"):
            return await interaction.response.send_message("<:samx_wrong:1497645159037276171> Already closed.", ephemeral=True)

        await interaction.response.send_message("<:samx_blue_gem_lock:1497645047951397085> Closing ticket...", ephemeral=True)
        await _close_ticket(interaction.channel, interaction.guild)

    @discord.ui.button(label="Claim", emoji="<:samx_crown:1497645120848396519>", style=discord.ButtonStyle.blurple, custom_id="ticket_action_claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        staff_role = interaction.guild.get_role(TICKET_STAFF_ROLE_ID)
        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("<:samx_wrong:1497645159037276171> Only ticket support staff can claim tickets.", ephemeral=True)

        data = ticket_data.setdefault(interaction.channel.id, {})
        if data.get("claimer"):
            claimer = interaction.guild.get_member(data["claimer"])
            return await interaction.response.send_message(f"<:samx_wrong:1497645159037276171> Already claimed by {claimer.mention if claimer else 'someone'}.", ephemeral=True)

        data["claimer"] = interaction.user.id
        save_ticket_data()

        embed = discord.Embed(
            title="<:samx_crown:1497645120848396519> Ticket Claimed",
            description="This ticket has been claimed and is now being handled.",
            color=0x3498db,
        )
        embed.add_field(name="<:samx_dcuser:1497644961858977912> Staff Member", value=interaction.user.mention, inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Claimed by {interaction.user} • {interaction.user.id}")
        await interaction.response.send_message(embed=embed)


# ── Modals ─────────────────────────────────────────────────────────────────────

# ── Robux flow: Modal → Type select → Payment select → ticket created ──────────

class RobuxModal(discord.ui.Modal, title="Robux Order"):
    item = discord.ui.TextInput(label="Item / Amount of Robux", placeholder="Ex: 1000 Robux", max_length=100)
    username = discord.ui.TextInput(label="Roblox Username", placeholder="Optional", required=False, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        # After modal submit → send ephemeral embed with Robux type dropdown
        embed = discord.Embed(
            title="<:samx_ROBLOX:1497644702504321225> Robux Order",
            description="What type of Robux is it?",
            color=0x3498db,
        )
        await interaction.response.send_message(
            embed=embed,
            view=RobuxTypeView(self.item.value, self.username.value or "Not provided", interaction),
            ephemeral=True,
        )


class RobuxTypeSelect(discord.ui.Select):
    def __init__(self, item: str, username: str, ephemeral_interaction: discord.Interaction):
        self.item = item
        self.username = username
        self.ephemeral_interaction = ephemeral_interaction
        options = [
            discord.SelectOption(label="Group Payout", emoji="<:samx_group:1498268791602151504>", value="group_payout"),
            discord.SelectOption(label="InGame Gifting", emoji="<:samx_roblox:1498268879200194600>", value="ingame_gifting"),
        ]
        super().__init__(placeholder="What type of Robux is it?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        robux_type = "Group Payout" if self.values[0] == "group_payout" else "InGame Gifting"
        embed = discord.Embed(
            title="<:samx_ROBLOX:1497644702504321225> Select Payment Method",
            description="Select your preferred payment method below.",
            color=0x3498db,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=RobuxPaymentView(robux_type, self.item, self.username, self.ephemeral_interaction),
        )


class RobuxTypeView(discord.ui.View):
    def __init__(self, item: str, username: str, ephemeral_interaction: discord.Interaction):
        super().__init__(timeout=120)
        self.add_item(RobuxTypeSelect(item, username, ephemeral_interaction))


class RobuxOtherPaymentModal(discord.ui.Modal, title="Custom Payment Method"):
    method = discord.ui.TextInput(label="Payment Method", placeholder="PayPal / Crypto", max_length=50)

    def __init__(self, robux_type: str, item: str, username: str, ephemeral_interaction: discord.Interaction):
        super().__init__()
        self.robux_type = robux_type
        self.item = item
        self.username = username
        self.ephemeral_interaction = ephemeral_interaction

    async def on_submit(self, interaction: discord.Interaction):
        ticket_channel = await _create_ticket_channel(
            interaction,
            category_id=ROBUX_CATEGORY_ID,
            product=f"Robux - {self.robux_type}",
            details_desc=(
                f"**Item**\n```{self.item}```\n"
                f"**Roblox Username**\n```{self.username}```\n"
                f"**Robux Type**\n```{self.robux_type}```\n"
                f"**Payment Method**\n```{self.method.value}```"
            ),
        )
        done_embed = discord.Embed(
            title="<:samx_tick:1497645191463440605> Ticket Created",
            description=f"Your ticket has been created: {ticket_channel.mention}",
            color=0x2ecc71,
        )
        # Acknowledge the modal submit silently, then edit the ephemeral embed
        await interaction.response.defer()
        await self.ephemeral_interaction.edit_original_response(embed=done_embed, view=None)


class RobuxPaymentSelect(discord.ui.Select):
    def __init__(self, robux_type: str, item: str, username: str):
        self.robux_type = robux_type
        self.item = item
        self.username = username
        options = [
            discord.SelectOption(label="Esewa", emoji="<:samx_esewa:1497644658162139297>", value="esewa"),
            discord.SelectOption(label="Khalti", emoji="<:samx_khalti:1498268381139177513>", value="khalti"),
            discord.SelectOption(label="Other", emoji="<:samx_Paypal:1498268421463474278>", value="other"),
        ]
        super().__init__(placeholder="Select your preferred payment method", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "other":
            await interaction.response.send_modal(
                RobuxOtherPaymentModal(self.robux_type, self.item, self.username, interaction)
            )
            return

        payment = self.values[0].capitalize()
        ticket_channel = await _create_ticket_channel(
            interaction,
            category_id=ROBUX_CATEGORY_ID,
            product=f"Robux - {self.robux_type}",
            details_desc=(
                f"**Item**\n```{self.item}```\n"
                f"**Roblox Username**\n```{self.username}```\n"
                f"**Robux Type**\n```{self.robux_type}```\n"
                f"**Payment Method**\n```{payment}```"
            ),
        )
        done_embed = discord.Embed(
            title="<:samx_tick:1497645191463440605> Ticket Created",
            description=f"Your ticket has been created: {ticket_channel.mention}",
            color=0x2ecc71,
        )
        await interaction.response.edit_message(embed=done_embed, view=None)


class RobuxPaymentView(discord.ui.View):
    def __init__(self, robux_type: str, item: str, username: str):
        super().__init__(timeout=120)
        self.add_item(RobuxPaymentSelect(robux_type, item, username))

class OtherModal(discord.ui.Modal, title="Product Order"):
    product = discord.ui.TextInput(label="What item do you wanna purchase?", placeholder="Ex: 2 Kitsune, YETI", max_length=100)
    username = discord.ui.TextInput(label="Your Roblox username", placeholder="Optional", required=False, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="<:samx_cart:1497644780539220018> Select Payment Method",
            description="Select your preferred payment method below.",
            color=0x3498db,
        )
        await interaction.response.send_message(
            embed=embed,
            view=OtherPaymentView(self.product.value, self.username.value or "Not provided", interaction),
            ephemeral=True,
        )


class OtherPaymentSelect(discord.ui.Select):
    def __init__(self, product: str, username: str, ephemeral_interaction: discord.Interaction):
        self.product = product
        self.username = username
        self.ephemeral_interaction = ephemeral_interaction
        options = [
            discord.SelectOption(label="Esewa", emoji="<:samx_esewa:1497644658162139297>", value="esewa"),
            discord.SelectOption(label="Khalti", emoji="<:samx_khalti:1498268381139177513>", value="khalti"),
            discord.SelectOption(label="Other", emoji="<:samx_Paypal:1498268421463474278>", value="other"),
        ]
        super().__init__(placeholder="Select your preferred payment method", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "other":
            await interaction.response.send_modal(
                OtherPaymentModal(self.product, self.username, self.ephemeral_interaction)
            )
            return

        payment = self.values[0].capitalize()
        ticket_channel = await _create_ticket_channel(
            interaction,
            category_id=OTHER_CATEGORY_ID,
            product=self.product,
            details_desc=(
                f"**Item**\n```{self.product}```\n"
                f"**Roblox Username**\n```{self.username}```\n"
                f"**Payment Method**\n```{payment}```"
            ),
        )
        done_embed = discord.Embed(
            title="<:samx_tick:1497645191463440605> Ticket Created",
            description=f"Your ticket has been created: {ticket_channel.mention}",
            color=0x2ecc71,
        )
        await interaction.response.edit_message(embed=done_embed, view=None)


class OtherPaymentModal(discord.ui.Modal, title="Custom Payment Method"):
    method = discord.ui.TextInput(label="Payment Method", placeholder="PayPal / Crypto", max_length=50)

    def __init__(self, product: str, username: str, ephemeral_interaction: discord.Interaction):
        super().__init__()
        self.product = product
        self.username = username
        self.ephemeral_interaction = ephemeral_interaction

    async def on_submit(self, interaction: discord.Interaction):
        ticket_channel = await _create_ticket_channel(
            interaction,
            category_id=OTHER_CATEGORY_ID,
            product=self.product,
            details_desc=(
                f"**Item**\n```{self.product}```\n"
                f"**Roblox Username**\n```{self.username}```\n"
                f"**Payment Method**\n```{self.method.value}```"
            ),
        )
        done_embed = discord.Embed(
            title="<:samx_tick:1497645191463440605> Ticket Created",
            description=f"Your ticket has been created: {ticket_channel.mention}",
            color=0x2ecc71,
        )
        await interaction.response.defer()
        await self.ephemeral_interaction.edit_original_response(embed=done_embed, view=None)


class OtherPaymentView(discord.ui.View):
    def __init__(self, product: str, username: str, ephemeral_interaction: discord.Interaction):
        super().__init__(timeout=120)
        self.add_item(OtherPaymentSelect(product, username, ephemeral_interaction))

async def _create_ticket_channel(interaction: discord.Interaction, category_id: int, product: str, details_desc: str) -> discord.TextChannel:
    guild = interaction.guild
    staff_role = guild.get_role(TICKET_STAFF_ROLE_ID)
    category = guild.get_channel(category_id)

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
        "product": product,
        "claimer": None,
    }
    save_ticket_data()

    welcome_embed = discord.Embed(
        title="<:samx_ticket_premium:1497645031836876982> Ticket Opened",
        description=f"Hey {interaction.user.mention}, thanks for reaching out!\nA staff member will be with you shortly.\n\n",
        color=0x3498db,
    )
    welcome_embed.set_thumbnail(url=bot.user.display_avatar.url)
    welcome_embed.set_footer(text=f"Ticket by {interaction.user} • {interaction.user.id}")

    details_embed = discord.Embed(title="<:samx_product:1497644984894226563> Order Details", color=0x3498db)
    details_embed.description = details_desc

    mentions = f"{interaction.user.mention} {staff_role.mention if staff_role else ''}"
    await ticket_channel.send(mentions, embeds=[welcome_embed, details_embed], view=TicketActionView())
    return ticket_channel


async def _create_ticket(interaction: discord.Interaction, category_id: int, product: str, details_desc: str, deferred: bool = False):
    ticket_channel = await _create_ticket_channel(interaction, category_id, product, details_desc)
    msg = f"<:samx_tick:1497645191463440605> Your ticket has been created: {ticket_channel.mention}"
    if deferred:
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

# ── Dropdown ───────────────────────────────────────────────────────────────────

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Robux Order", description="Purchase Robux or Roblox items", emoji="<:samx_ROBLOX:1497644702504321225>", value="robux"),
            discord.SelectOption(label="Other Product", description="Purchase any other product", emoji="<:samx_cart:1497644780539220018>", value="other"),
        ]
        super().__init__(placeholder="Click here to Purchase..", min_values=1, max_values=1, options=options, custom_id="ticket_panel_dropdown")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "robux":
            await interaction.response.send_modal(RobuxModal())
            await interaction.message.edit(view=TicketPanelView())
        elif self.values[0] == "other":
            await interaction.response.send_modal(OtherModal())
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
        description=(
            "# DR!X MARKET <a:HEHE:1495448735126126663>\n"
            "** ### <:good:1495673281892716645> Begin by submitting your item name in the order channel.\n"
            "### <:OK:1492771833156604045> Our staff will assist you as soon as possible.\n"
            " ###  <:YES:1495448593312780310> Professional • Efficient • Secure trading  **"
        ),
    )

    embed.set_image(url="attachment://banner.png")
    await interaction.response.send_message("<:samx_tick:1497645191463440605> Panel sent!", ephemeral=True)
    await interaction.channel.send(embed=embed, view=TicketPanelView(), file=discord.File("banner.png"))

@ticket_panel.error
async def ticket_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("<:samx_wrong:1497645159037276171> You need Administrator permissions.", ephemeral=True)

# ── Prefix: $claim ────────────────────────────────────────────────────────────

@bot.command(name="claim")
async def claim_ticket(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:samx_wrong:1497645159037276171> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:samx_wrong:1497645159037276171> Only ticket support staff can claim tickets.")

    data = ticket_data.setdefault(ctx.channel.id, {})
    if data.get("claimer"):
        claimer = ctx.guild.get_member(data["claimer"])
        return await ctx.send(f"<:samx_wrong:1497645159037276171> Already claimed by {claimer.mention if claimer else 'someone'}.")

    data["claimer"] = ctx.author.id
    save_ticket_data()
    await ctx.message.delete()

    embed = discord.Embed(
        title="<:samx_crown:1497645120848396519> Ticket Claimed",
        description="This ticket has been claimed and is now being handled.",
        color=0x3498db,
    )
    embed.add_field(name="<:samx_dcuser:1497644961858977912> Staff Member", value=ctx.author.mention, inline=True)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.set_footer(text=f"Claimed by {ctx.author} • {ctx.author.id}")
    await ctx.channel.send(embed=embed)

# ── Prefix: $remind ───────────────────────────────────────────────────────────

@bot.command(name="remind")
async def remind(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:samx_wrong:1497645159037276171> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:samx_wrong:1497645159037276171> Only staff can send reminders.")

    data = ticket_data.get(ctx.channel.id, {})
    opener_id = data.get("opener")
    opener = ctx.guild.get_member(opener_id) if opener_id else None

    if not opener:
        return await ctx.send("<:samx_wrong:1497645159037276171> Could not find the ticket opener.")

    ticket_url = f"https://discord.com/channels/{ctx.guild.id}/{ctx.channel.id}"

    embed = discord.Embed(
        title="<:samx_blue_clock:1497645234530550002> Ticket Reminder",
        description=f"Hey {opener.mention}, you have an open ticket waiting for your response!",
        color=0x3498db,
    )
    embed.add_field(name="Ticket", value=ctx.channel.name, inline=True)
    embed.add_field(name="Staff", value=ctx.author.mention, inline=True)
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="Please respond as soon as possible.")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Go to Ticket", url=ticket_url, emoji="<:samx_blue_clock:1497645234530550002>"))

    try:
        await opener.send(embed=embed, view=view)
        await ctx.message.delete()
        await ctx.channel.send(f"<:samx_tick:1497645191463440605> Reminder sent to {opener.mention}.", delete_after=5)
    except discord.Forbidden:
        await ctx.send("<:samx_wrong:1497645159037276171> Could not DM the ticket opener — they may have DMs disabled.")

# ── Prefix: $add ──────────────────────────────────────────────────────────────

@bot.command(name="add")
async def add_user(ctx: commands.Context, user: discord.Member):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:samx_wrong:1497645159037276171> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:samx_wrong:1497645159037276171> Only staff can add users.")

    await ctx.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    ticket_data.setdefault(ctx.channel.id, {}).setdefault("added_users", set()).add(user.id)
    save_ticket_data()
    await ctx.send(f"<:samx_tick:1497645191463440605> {user.mention} has been added to the ticket.")

# ── Prefix: $remove ───────────────────────────────────────────────────────────

@bot.command(name="remove")
async def remove_user(ctx: commands.Context, user: discord.Member):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:samx_wrong:1497645159037276171> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:samx_wrong:1497645159037276171> Only staff can remove users.")

    await ctx.channel.set_permissions(user, overwrite=None)
    ticket_data.get(ctx.channel.id, {}).get("added_users", set()).discard(user.id)
    save_ticket_data()
    await ctx.send(f"<:samx_tick:1497645191463440605> {user.mention} has been removed from the ticket.")

# ── Prefix: $close ────────────────────────────────────────────────────────────

@bot.command(name="close", aliases=["cl", "Cl"])
async def close_ticket(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:samx_wrong:1497645159037276171> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:samx_wrong:1497645159037276171> Only staff can close tickets.")
    if ctx.channel.name.startswith("closed-"):
        return await ctx.send("<:samx_wrong:1497645159037276171> This ticket is already closed.")

    await ctx.message.delete()
    await _close_ticket(ctx.channel, ctx.guild)

# ── Prefix: $close.v ──────────────────────────────────────────────────────────

@bot.command(name="close.v", aliases=["cv", "Cv"])
async def close_vouch(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("<:samx_wrong:1497645159037276171> This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("<:samx_wrong:1497645159037276171> Only staff can close tickets.")
    if ctx.channel.name.startswith("closed-"):
        return await ctx.send("<:samx_wrong:1497645159037276171> This ticket is already closed.")

    data = ticket_data.get(ctx.channel.id, {})
    opener_id = data.get("opener")
    opener = ctx.guild.get_member(opener_id) if opener_id else None

    vouch_embed = discord.Embed(
        title="<:samx_crown:1497645120848396519>  DR!X MARKET VOUCH !",
        description=(
            f"Thank you {opener.mention if opener else 'valued customer'} for purchasing **{data.get('product', 'your order')}** in our server <:heart:1495338641508270110>\n\n"
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
        emoji="<:samx_heart:1497644727238135919>",
        url="https://discordapp.com/channels/1480754399075635292/1488913327575797911",
    ))

    content = f"{opener.mention}"
    await ctx.message.delete()
    await _close_ticket(ctx.channel, ctx.guild, vouch_embed=vouch_embed, vouch_view=vouch_view)

# ── Bot events ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def ping_self():
    try:
        async with aiohttp.ClientSession() as session:
            await session.get(KEEP_ALIVE_URL)
    except Exception:
        pass

@bot.event
async def on_ready():
    load_ticket_data()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())
    bot.add_view(TicketActionView())
    await bot.tree.sync()
    ping_self.start()
    print(f"Logged in as {bot.user} | Synced slash commands")

bot.run(TOKEN)
