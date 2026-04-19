import discord
from discord.ext import commands
from discord import app_commands
import os
import io
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

# ticket_opener stores channel_id -> opener user_id
ticket_openers: dict[int, int] = {}
# added_users stores channel_id -> set of user_ids added via $add
added_users: dict[int, set[int]] = {}

# ── Helpers ────────────────────────────────────────────────────────────────────

def is_ticket_channel(channel: discord.TextChannel) -> bool:
    return channel.name.startswith("ticket-")

async def generate_transcript(channel: discord.TextChannel) -> discord.File | None:
    export = await chat_exporter.export(channel)
    if export is None:
        return None
    return discord.File(
        io.BytesIO(export.encode()),
        filename=f"transcript-{channel.name}.html",
    )

# ── Close/Delete View ──────────────────────────────────────────────────────────

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔁 Reopen", style=discord.ButtonStyle.green, custom_id="ticket_reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        opener_id = ticket_openers.get(channel.id)
        staff_role = interaction.guild.get_role(TICKET_STAFF_ROLE_ID)

        # Restore opener perms
        if opener_id:
            opener = interaction.guild.get_member(opener_id)
            if opener:
                await channel.set_permissions(opener, view_channel=True, send_messages=True, read_message_history=True)

        # Restore added users
        for uid in added_users.get(channel.id, set()):
            member = interaction.guild.get_member(uid)
            if member:
                await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)

        await channel.edit(name=channel.name.replace("closed-", ""))
        await interaction.response.send_message("✅ Ticket reopened.", ephemeral=True)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.red, custom_id="ticket_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        staff_role = interaction.guild.get_role(TICKET_STAFF_ROLE_ID)

        # Only staff can delete
        if staff_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only staff can delete tickets.", ephemeral=True)
            return

        await interaction.response.send_message("🗑️ Deleting ticket...", ephemeral=True)

        # If not already closed, close first (send transcript)
        if not channel.name.startswith("closed-"):
            await _close_ticket(channel, interaction.guild)

        ticket_openers.pop(channel.id, None)
        added_users.pop(channel.id, None)
        await channel.delete()

# ── Close logic (shared) ───────────────────────────────────────────────────────

async def _close_ticket(channel: discord.TextChannel, guild: discord.Guild):
    opener_id = ticket_openers.get(channel.id)
    staff_role = guild.get_role(TICKET_STAFF_ROLE_ID)

    # Generate & send transcript
    transcript_file = await generate_transcript(channel)
    transcript_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)

    if transcript_file and transcript_channel:
        t_embed = discord.Embed(
            title="📄 Transcript",
            description=f"Ticket: `{channel.name}`",
            color=0x3498db,
        )
        if opener_id:
            opener = guild.get_member(opener_id)
            if opener:
                t_embed.add_field(name="Opened by", value=opener.mention)
        await transcript_channel.send(embed=t_embed, file=transcript_file)

    # DM transcript to opener
    if opener_id:
        opener = guild.get_member(opener_id)
        if opener:
            transcript_file2 = await generate_transcript(channel)
            try:
                dm_embed = discord.Embed(
                    title="📄 Your Ticket Transcript",
                    description=f"Your ticket `{channel.name}` has been closed.",
                    color=0x3498db,
                )
                await opener.send(embed=dm_embed, file=transcript_file2)
            except discord.Forbidden:
                pass

    # Remove opener & added users from channel
    if opener_id:
        opener = guild.get_member(opener_id)
        if opener:
            await channel.set_permissions(opener, overwrite=None)

    for uid in added_users.get(channel.id, set()):
        member = guild.get_member(uid)
        if member:
            await channel.set_permissions(member, overwrite=None)

    await channel.edit(name=f"closed-{channel.name}")

    # Send close embed with controls
    close_embed = discord.Embed(
        title="🔒 Ticket Closed",
        description="This ticket has been closed. Staff can reopen or delete it below.",
        color=0x3498db,
    )
    await channel.send(embed=close_embed, view=TicketControlView())

# ── Modal ──────────────────────────────────────────────────────────────────────

class PurchaseModal(discord.ui.Modal, title="Purchase Request"):
    product = discord.ui.TextInput(label="Product", placeholder="What would you like to purchase?", max_length=100)
    qty = discord.ui.TextInput(label="Quantity", placeholder="How many?", max_length=10)
    budget = discord.ui.TextInput(label="Budget", placeholder="Your budget (e.g. $50)", max_length=50)
    payment_method = discord.ui.TextInput(label="Payment Method", placeholder="PayPal, Crypto, Card, etc.", max_length=50)

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

        ticket_openers[ticket_channel.id] = interaction.user.id
        added_users[ticket_channel.id] = set()

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
            f"**Quantity**\n```{self.qty.value}```\n"
            f"**Budget**\n```{self.budget.value}```\n"
            f"**Payment Method**\n```{self.payment_method.value}```"
        )

        mentions = f"{interaction.user.mention} {staff_role.mention if staff_role else ''}"
        await ticket_channel.send(mentions, embeds=[welcome_embed, details_embed])
        await interaction.response.send_message(f"✅ Your ticket has been created: {ticket_channel.mention}", ephemeral=True)

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
    embed.set_footer(text="We'll get back to you as soon as possible.")
    await interaction.response.send_message(embed=embed, view=TicketPanelView())

@ticket_panel.error
async def ticket_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need Administrator permissions.", ephemeral=True)

# ── Prefix: $add ──────────────────────────────────────────────────────────────

@bot.command(name="add")
async def add_user(ctx: commands.Context, user: discord.Member):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("❌ This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Only staff can add users.")

    await ctx.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    added_users.setdefault(ctx.channel.id, set()).add(user.id)
    await ctx.send(f"✅ {user.mention} has been added to the ticket.")

# ── Prefix: $remove ───────────────────────────────────────────────────────────

@bot.command(name="remove")
async def remove_user(ctx: commands.Context, user: discord.Member):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("❌ This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Only staff can remove users.")

    await ctx.channel.set_permissions(user, overwrite=None)
    added_users.get(ctx.channel.id, set()).discard(user.id)
    await ctx.send(f"✅ {user.mention} has been removed from the ticket.")

# ── Prefix: $close ────────────────────────────────────────────────────────────

@bot.command(name="close")
async def close_ticket(ctx: commands.Context):
    if not is_ticket_channel(ctx.channel):
        return await ctx.send("❌ This is not a ticket channel.")
    staff_role = ctx.guild.get_role(TICKET_STAFF_ROLE_ID)
    if staff_role not in ctx.author.roles and not ctx.author.guild_permissions.administrator:
        return await ctx.send("❌ Only staff can close tickets.")
    if ctx.channel.name.startswith("closed-"):
        return await ctx.send("❌ This ticket is already closed.")

    await ctx.message.delete()
    await _close_ticket(ctx.channel, ctx.guild)

# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())
    await bot.tree.sync()
    print(f"Logged in as {bot.user} | Synced slash commands")

bot.run(TOKEN)
