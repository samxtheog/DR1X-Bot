import discord
from discord.ext import commands
from discord import app_commands
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
TICKET_STAFF_ROLE_ID = int(os.getenv("TICKET_STAFF_ROLE_ID"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID"))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Modal ──────────────────────────────────────────────────────────────────────

class PurchaseModal(discord.ui.Modal, title="Purchase Request"):
    product = discord.ui.TextInput(
        label="Product",
        placeholder="What would you like to purchase?",
        max_length=100,
    )
    qty = discord.ui.TextInput(
        label="Quantity",
        placeholder="How many?",
        max_length=10,
    )
    budget = discord.ui.TextInput(
        label="Budget",
        placeholder="Your budget (e.g. $50)",
        max_length=50,
    )
    payment_method = discord.ui.TextInput(
        label="Payment Method",
        placeholder="PayPal, Crypto, Card, etc.",
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        staff_role = guild.get_role(TICKET_STAFF_ROLE_ID)
        category = guild.get_channel(TICKET_CATEGORY_ID)

        # Create ticket channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites,
        )

        # Welcome embed
        welcome_embed = discord.Embed(
            title="<:bluetada:1495326072391077908> Ticket Opened",
            description=(
                f"Hey {interaction.user.mention}, thanks for reaching out!\n"
                f"A staff member will be with you shortly.\n\n"
                f"{staff_role.mention if staff_role else ''}"
            ),
            color=discord.Color.blurple(),
        )
        welcome_embed.set_footer(text=f"Ticket by {interaction.user} • {interaction.user.id}")

        # Details embed
        details_embed = discord.Embed(
            title="<:product:1495325856854179922> Order Details",
            color=discord.Color.dark_grey(),
        )
        details_embed.description = (
            "```\n"
            f"Product        : {self.product.value}\n"
            f"Quantity       : {self.qty.value}\n"
            f"Budget         : {self.budget.value}\n"
            f"Payment Method : {self.payment_method.value}\n"
            "```"
        )

        await ticket_channel.send(embeds=[welcome_embed, details_embed])

        await interaction.response.send_message(
            f"✅ Your ticket has been created: {ticket_channel.mention}",
            ephemeral=True,
        )


# ── Dropdown ───────────────────────────────────────────────────────────────────

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Purchase Now",
                description="Open a purchase request ticket",
                emoji="<:_cart:1495325346218901574>",
                value="purchase",
            ),
        ]
        super().__init__(
            placeholder="Select an option...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_panel_dropdown", 
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "purchase":
            await interaction.response.send_modal(PurchaseModal())


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  
        self.add_item(TicketDropdown())


# ── Command ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="ticket-panel", description="Send the ticket panel embed")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="<:_cart:1495325346218901574> DR!X MARKET",
        description=(
            "Need help or want to place an order?\n"
            "Select an option from the dropdown below to open a ticket."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="We'll get back to you as soon as possible.")

    await interaction.response.send_message(embed=embed, view=TicketPanelView())


@ticket_panel.error
async def ticket_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need Administrator permissions.", ephemeral=True)


# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    # Re-register persistent view on restart
    bot.add_view(TicketPanelView())
    await bot.tree.sync()
    print(f"Logged in as {bot.user} | Synced slash commands")


bot.run(TOKEN)
