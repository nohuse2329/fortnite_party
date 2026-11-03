import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import os
from aiohttp import web

# --- Botの初期設定 ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------
# ⚙️ モードリスト（初期設定・サーバー内で管理・並び替え可能）
# ---------------------------------------------------------
DEFAULT_MODES = [
    "ガチパイレーツショップ周回",
    "リロード",
    "リロードランク",
    "バトロワ",
    "バトロワランク",
    "クリエマップ",
    "大会"
]

DYNAMIC_CONFIG = {
    "modes": DEFAULT_MODES.copy()
}

parties = {}
party_counter = 0
invite_cooldowns = {}
log_channel_id = None

async def send_log(guild: discord.Guild, embed: discord.Embed):
    global log_channel_id
    if log_channel_id:
        ch = guild.get_channel(log_channel_id)
        if ch:
            try: await ch.send(embed=embed)
            except: pass

@bot.event
async def on_member_join(member: discord.Member):
    embed = discord.Embed(
        title="📥 メンバー参加",
        description=f"{member.mention} (`{member.name}`) が参加しました。",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    await send_log(member.guild, embed)

@bot.event
async def on_member_remove(member: discord.Member):
    embed = discord.Embed(
        title="📤 メンバー退出",
        description=f"{member.mention} (`{member.name}`) が退出しました。",
        color=discord.Color.red(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    await send_log(member.guild, embed)

# --- PT作成用モーダル＆ビュー ---
class CreatePartyWizardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.selected_mode = DYNAMIC_CONFIG["modes"][0] if DYNAMIC_CONFIG["modes"] else "バトロワ"
        self.selected_cap = 3  # デフォルト募集枠（3人追加募集＝4人PT）

        if DYNAMIC_CONFIG["modes"]:
            self.add_item(ModeSelect([discord.SelectOption(label=m, value=m) for m in DYNAMIC_CONFIG["modes"][:25]]))
        self.add_item(CapSelect())

    @discord.ui.button(label="📝 パーティー名を入力して作成", style=discord.ButtonStyle.success, row=2)
    async def open_modal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyNameModal(self.selected_mode, self.selected_cap))

class ModeSelect(discord.ui.Select):
    def __init__(self, options): super().__init__(placeholder="① モードを選択...", options=options, row=0)
    async def callback(self, interaction: discord.Interaction):
        self.view.selected_mode = self.values[0]
        await interaction.response.defer()

class CapSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=f"募集: {i}名", value=str(i)) for i in range(1, 11)]
        super().__init__(placeholder="② 募集人数を選択 (1〜10名)...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_cap = int(self.values[0])
        await interaction.response.defer()

class PartyNameModal(discord.ui.Modal, title='パーティー名入力'):
    def __init__(self, mode, cap):
        super().__init__()
        self.mode = mode
        self.cap = cap

    party_title = discord.ui.TextInput(
        label='パーティー名（目的など自由に入力）',
        placeholder='例: @1 周回できる人！ / 楽しくやろう！',
        max_length=50,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        global party_counter
        guild = interaction.guild
        user = interaction.user

        party_counter += 1
        p_id = party_counter

        max_members = self.cap + 1  # 募集人数 + リーダー

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False, connect=False),
            user: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True, manage_channels=True)
        }

        category = interaction.channel.category
        text_channel = await guild.create_text_channel(name=f"pt-{p_id}-{user.name}", category=category, overwrites=overwrites)
        voice_channel = await guild.create_voice_channel(name=f"🔊 PT-{p_id} ({user.name})", category=category, user_limit=max_members, overwrites=overwrites)

        parties[p_id] = {
            "id": p_id,
            "title": self.party_title.value,
            "leader": user,
            "mode": self.mode,
            "req_cap": self.cap,
            "max_members": max_members,
            "members": [user],
            "text_channel": text_channel,
            "voice_channel": voice_channel,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "notified": False
        }

        embed = discord.Embed(
            title=f"🎮 【{self.party_title.value}】",
            description=f"# 📢 **{self.party_title.value}**\n\n"
                        f"🎮 **モード:** `{self.mode}`\n"
                        f"👤 **募集者:** {user.mention}\n"
                        f"👥 **募集人数:** `{self.cap}名` (現在: 1/{max_members}名)\n"
                        f"🔊 **専用VC:** {voice_channel.mention}\n",
            color=discord.Color.green()
        )
        await text_channel.send(content=f"{user.mention} パーティーが作成されました！", embed=embed, view=PartyControlView(p_id))
        await interaction.response.send_message(f"✅ パーティーを作成しました！ {text_channel.mention}", ephemeral=True)

# --- PT内での操作ボタン ---
class PartyControlView(discord.ui.View):
    def __init__(self, party_id):
        super().__init__(timeout=None)
        self.party_id = party_id

    @discord.ui.button(label="✉️ 招待", style=discord.ButtonStyle.primary)
    async def invite_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p or interaction.user != p["leader"]:
            return await interaction.response.send_message("リーダーのみ招待可能です。", ephemeral=True)
        if len(p["members"]) >= p["max_members"]:
            return await interaction.response.send_message("既に満員です。", ephemeral=True)

        guild_members = [m for m in interaction.guild.members if not m.bot and m not in p["members"]]
        view = discord.ui.View()
        view.add_item(AllMemberInviteSelect(self.party_id, guild_members))
        await interaction.response.send_message("招待するメンバーを選択してください:", view=view, ephemeral=True)

    @discord.ui.button(label="👑 リーダー譲渡", style=discord.ButtonStyle.secondary)
    async def transfer_leader(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p or interaction.user != p["leader"]:
            return await interaction.response.send_message("権限がありません。", ephemeral=True)
        other_members = [m for m in p["members"] if m != p["leader"]]
        if not other_members:
            return await interaction.response.send_message("譲渡できるメンバーがいません。", ephemeral=True)
        view = discord.ui.View()
        view.add_item(MemberSelect(self.party_id, other_members))
        await interaction.response.send_message("新リーダーを選択:", view=view, ephemeral=True)

    @discord.ui.button(label="🚪 脱退", style=discord.ButtonStyle.secondary)
    async def leave_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p: return await interaction.response.send_message("パーティーが存在しません。", ephemeral=True)
        user = interaction.user
        if user not in p["members"]: return await interaction.response.send_message("参加していません。", ephemeral=True)
        if user == p["leader"]: return await interaction.response.send_message("リーダーは脱退できません（解散するか権限を譲渡してください）。", ephemeral=True)

        p["members"].remove(user)
        try:
            await p["text_channel"].set_permissions(user, overwrite=None)
            await p["voice_channel"].set_permissions(user, overwrite=None)
        except: pass

        await p["text_channel"].send(f"🚪 {user.mention} が脱退しました。")
        await interaction.response.send_message("脱退しました。", ephemeral=True)

    @discord.ui.button(label="💥 解散（管理者/リーダー）", style=discord.ButtonStyle.danger)
    async def delete_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        is_admin = interaction.user.guild_permissions.administrator
        if not p or (interaction.user != p["leader"] and not is_admin):
            return await interaction.response.send_message("❌ 権限がありません（リーダーまたは管理者のみ実行可能）。", ephemeral=True)

        await interaction.response.send_message("解散中...")
        await asyncio.sleep(1)
        try:
            await p["text_channel"].delete()
            await p["voice_channel"].delete()
        except: pass
        parties.pop(self.party_id, None)

class AllMemberInviteSelect(discord.ui.Select):
    def __init__(self, party_id, members):
        self.party_id = party_id
        options = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members[:25]]
        super().__init__(placeholder="メンバーを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        p = parties.get(self.party_id)
        if not p: return
        target_id = int(self.values[0])
        target_member = interaction.guild.get_member(target_id)
        if not target_member: return

        view = PartyInviteAcceptView(self.party_id, target_member.id)
        embed = discord.Embed(
            title="✉️ パーティー招待",
            description=f"{target_member.mention} さん\n{interaction.user.mention} から **「{p['title']}」** への招待が届きました。",
            color=discord.Color.gold()
        )
        await p["text_channel"].send(content=f"{target_member.mention}", embed=embed, view=view)
        await interaction.response.send_message("招待通知を送りました。", ephemeral=True)

class PartyInviteAcceptView(discord.ui.View):
    def __init__(self, party_id, target_user_id):
        super().__init__(timeout=None)
        self.party_id = party_id
        self.target_user_id = target_user_id

    @discord.ui.button(label="✅ 参加する", style=discord.ButtonStyle.success)
    async def accept_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p or interaction.user.id != self.target_user_id:
            return await interaction.response.send_message("無効な操作です。", ephemeral=True)
        if len(p["members"]) >= p["max_members"]:
            return await interaction.response.send_message("満員です。", ephemeral=True)

        p["members"].append(interaction.user)
        perm = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True)
        await p["text_channel"].set_permissions(interaction.user, overwrite=perm)
        await p["voice_channel"].set_permissions(interaction.user, overwrite=perm)

        for c in self.children: c.disabled = True
        await interaction.message.edit(view=self)
        await p["text_channel"].send(f"🎉 {interaction.user.mention} が参加しました！")
        await interaction.response.send_message("参加しました！", ephemeral=True)

class MemberSelect(discord.ui.Select):
    def __init__(self, party_id, members):
        self.party_id = party_id
        options = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members]
        super().__init__(placeholder="新リーダーを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        p = parties.get(self.party_id)
        if p:
            new_l = interaction.guild.get_member(int(self.values[0]))
            p["leader"] = new_l
            await interaction.response.send_message(f"👑 {new_l.mention} にリーダー権限を譲渡しました！", ephemeral=True)

# --- メインパネル ---
class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎮 PT作成", style=discord.ButtonStyle.green, custom_id="fn_create")
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "📋 モードと募集人数を選択してください：",
            view=CreatePartyWizardView(),
            ephemeral=True
        )

    @discord.ui.button(label="📋 PT一覧・参加", style=discord.ButtonStyle.primary, custom_id="fn_list")
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not parties:
            return await interaction.response.send_message("現在募集中のパーティーはありません。", ephemeral=True)

        embed = discord.Embed(
            title="🏆【PARTY LIST】募集中のパーティー一覧",
            description="現在募集が行われているパーティーの一覧です。\n"
                        "----------------------------------------",
            color=discord.Color.gold()
        )

        join_options = []

        for p_id, p in list(parties.items()):
            is_full = len(p["members"]) >= p["max_members"]
            status_tag = "🔴 **[締切 / 満員]**" if is_full else "🟢 **[募集中]**"

            field_title = f"{status_tag} ID:{p_id} | 📢 {p['title']}"
            field_value = (
                f"# **{p['title']}**\n"
                f"🎮 **モード:** `{p['mode']}`\n"
                f"👤 **募集者:** {p['leader'].mention}\n"
                f"👥 **募集枠:** `{len(p['members'])} / {p['max_members']}名` (あと{p['max_members'] - len(p['members'])}名)\n"
                f"----------------------------------------"
            )
            embed.add_field(name=field_title, value=field_value, inline=False)

            if not is_full:
                join_options.append(discord.SelectOption(
                    label=f"ID:{p_id} | {p['title']} ({p['mode']})",
                    value=str(p_id)
                ))

        view = discord.ui.View()
        if join_options:
            view.add_item(PartyJoinSelect(join_options))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, content="※現在参加可能な（満員でない）募集はありません。", ephemeral=True)

class PartyJoinSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="👉 参加したいパーティーを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        p = parties.get(int(self.values[0]))
        user = interaction.user
        if not p: return

        if user in p["members"]: return await interaction.response.send_message("すでに追加されています。", ephemeral=True)

        p["members"].append(user)
        perm = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True)
        await p["text_channel"].set_permissions(user, overwrite=perm)
        await p["voice_channel"].set_permissions(user, overwrite=perm)

        await p["text_channel"].send(f"🎉 {user.mention} が参加しました！")
        await interaction.response.send_message(f"参加しました！ {p['text_channel'].mention}", ephemeral=True)

# ---------------------------------------------------------
# 🛠️ 管理者機能：文字入力なしでモード並び替え・削除・追加
# ---------------------------------------------------------
class ManageModesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ モード新規追加", style=discord.ButtonStyle.green, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddModeModal())

    @discord.ui.button(label="🗑️ モード一括削除", style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not DYNAMIC_CONFIG["modes"]:
            return await interaction.response.send_message("削除できるモードがありません。", ephemeral=True)
        view = discord.ui.View()
        view.add_item(RemoveModeSelect())
        await interaction.response.send_message("削除したいモードをチェック（選択）してください：", view=view, ephemeral=True)

    @discord.ui.button(label="🔄 モード一括並び替え", style=discord.ButtonStyle.primary, row=0)
    async def reorder_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not DYNAMIC_CONFIG["modes"]:
            return await interaction.response.send_message("並び替えるモードがありません。", ephemeral=True)
        current_str = "\n".join([f"{i+1}. {m}" for i, m in enumerate(DYNAMIC_CONFIG["modes"])])
        await interaction.response.send_modal(ReorderModesModal(current_str))

    @discord.ui.button(label="↺ 初期状態に戻す", style=discord.ButtonStyle.secondary, row=0)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        DYNAMIC_CONFIG["modes"] = DEFAULT_MODES.copy()
        await interaction.response.send_message("✅ モードリストを初期状態（ご指定の7種）に戻しました！", ephemeral=True)

class AddModeModal(discord.ui.Modal, title='モード追加'):
    mode_name = discord.ui.TextInput(label='追加するモード名', placeholder='例: アリーナ', max_length=30)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.mode_name.value.strip()
        if val and val not in DYNAMIC_CONFIG["modes"]:
            DYNAMIC_CONFIG["modes"].append(val)
            await interaction.response.send_message(f"✅ **{val}** をモード一覧に追加しました！", ephemeral=True)
        else:
            await interaction.response.send_message("既にあるか、無効な名前です。", ephemeral=True)

class RemoveModeSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=m, value=m) for m in DYNAMIC_CONFIG["modes"][:25]]
        super().__init__(placeholder="削除するモードを選択（複数選択可）...", options=options, min_values=1, max_values=len(options))

    async def callback(self, interaction: discord.Interaction):
        for val in self.values:
            if val in DYNAMIC_CONFIG["modes"]:
                DYNAMIC_CONFIG["modes"].remove(val)
        await interaction.response.send_message(f"🗑️ 選択したモード ({', '.join(self.values)}) を削除しました！", ephemeral=True)

class ReorderModesModal(discord.ui.Modal, title='モード並び替え'):
    def __init__(self, current_str):
        super().__init__()
        self.text_input = discord.ui.TextInput(
            label='現在の並び（行を入れ替えて保存してください）',
            style=discord.TextStyle.paragraph,
            default=current_str,
            required=True
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        lines = [line.strip() for line in self.text_input.value.split('\n') if line.strip()]
        new_modes = []
        for line in lines:
            # 「1. 」などの先頭番号がついている場合を除外して名前を取得
            cleaned = line.split('.', 1)[-1].strip() if '.' in line and line.split('.', 1)[0].isdigit() else line
            if cleaned:
                new_modes.append(cleaned)

        DYNAMIC_CONFIG["modes"] = new_modes
        await interaction.response.send_message("✅ モードの並び替えを保存しました！", ephemeral=True)

@bot.tree.command(name="manage_modes", description="[管理者] モードの追加・並び替え・一括削除メニューを開く")
@app_commands.checks.has_permissions(administrator=True)
async def manage_modes(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛠️ モード管理メニュー",
        description="打ち込みなしでボタンやドラッグで管理できます：\n\n" +
                    "\n".join([f"• **{m}**" for m in DYNAMIC_CONFIG["modes"]]),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=ManageModesView(), ephemeral=True)

@bot.tree.command(name="setup_log_channel", description="[管理者] サーバー入退室通知の送信先チャンネルを設定")
@app_commands.checks.has_permissions(administrator=True)
async def setup_log_channel(interaction: discord.Interaction):
    global log_channel_id
    log_channel_id = interaction.channel_id
    await interaction.response.send_message(f"✅ このチャンネル ({interaction.channel.mention}) を通知先に設定しました！", ephemeral=True)

@bot.tree.command(name="setup_fortnite_panel", description="[管理者] 募集パネル設置")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ パーティー募集＆総合管理パネル",
        description="下のボタンからパーティーの作成・一覧確認・参加ができます！\n\n"
                    "🎮 **PT作成:** モードと人数を選び、パーティー名を入力して募集開始\n"
                    "📋 **PT一覧・参加:** 現在募集中のパーティーを確認・参加",
        color=discord.Color.dark_purple()
    )
    await interaction.channel.send(embed=embed, view=MainPanelView())
    await interaction.response.send_message("パネルの設置が完了しました。", ephemeral=True)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    check_party_expiration.start()
    await bot.tree.sync()

@tasks.loop(minutes=5)
async def check_party_expiration():
    now = datetime.datetime.now(datetime.timezone.utc)
    for p_id, p in list(parties.items()):
        elapsed = now - p["created_at"]
        if elapsed >= datetime.timedelta(hours=48) and not p["notified"]:
            p["notified"] = True
            try: await p["text_channel"].send("⚠️ 48時間経過。2時間後に自動削除されます。")
            except: pass
        if elapsed >= datetime.timedelta(hours=50):
            try:
                await p["text_channel"].delete()
                await p["voice_channel"].delete()
            except: pass
            parties.pop(p_id, None)

# --- Render用Webサーバー ---
async def handle(request): return web.Response(text="Bot is running!")
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token: return
    await start_web_server()
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
