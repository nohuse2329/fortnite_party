import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import random
import os
from aiohttp import web

# --- Botの初期設定 ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------
# ⚙️ サーバー内で動的に編集可能な選択肢リスト
# ---------------------------------------------------------
DYNAMIC_CONFIG = {
    "games": ["フォートナイト", "Fortnite ZeroBuild", "その他"],
    "modes": [
        "🔥 ランク上げ（本気）",
        "☕ エンジョイ / 雑談",
        "🎯 クリエイティブ / 練習",
        "🏆 大会 / スクエイム",
        "👑 Victory Crown稼ぎ"
    ],
    "capacity": ["1人", "2人", "3人", "無制限"],
    "ranks": [
        "制限なし",
        "ブロンズ以上",
        "シルバー以上",
        "ゴールド以上",
        "プラチナ以上",
        "ダイヤモンド以上",
        "エリート以上",
        "チャンピオン以上",
        "アンリアル"
    ]
}

USER_RANKS = ["制限なし", "ブロンズ", "シルバー", "ゴールド", "プラチナ", "ダイヤモンド", "エリート", "チャンピオン", "アンリアル"]

RANK_HIERARCHY = {
    "制限なし": 0, "ブロンズ": 1, "シルバー": 2, "ゴールド": 3,
    "プラチナ": 4, "ダイヤモンド": 5, "エリート": 6, "チャンピオン": 7, "アンリアル": 8,
    "ブロンズ以上": 1, "シルバー以上": 2, "ゴールド以上": 3,
    "プラチナ以上": 4, "ダイヤモンド以上": 5, "エリート以上": 6, "チャンピオン以上": 7, "アンリアル以上": 8
}

user_link_data = {}
parties = {}
party_counter = 0
giveaways = {}
giveaway_counter = 0
invite_cooldowns = {}
log_channel_id = None

def get_user_data(user_id):
    if user_id not in user_link_data:
        user_link_data[user_id] = {"rank": "制限なし"}
    return user_link_data[user_id]

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

# --- ランク設定 ---
class SetRankSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        options = [discord.SelectOption(label=r, value=r) for r in USER_RANKS]
        self.add_item(SetRankSelect(options))

class SetRankSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="自分の最高ランクを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_rank = self.values[0]
        u_data = get_user_data(interaction.user.id)
        u_data["rank"] = selected_rank
        await interaction.response.send_message(f"✅ ランクを **{selected_rank}** に設定しました！", ephemeral=True)

# --- PT作成フロー（すべて選択式） ---
class CreatePartyWizardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.game = DYNAMIC_CONFIG["games"][0]
        self.mode = DYNAMIC_CONFIG["modes"][0]
        self.req_rank = DYNAMIC_CONFIG["ranks"][0]
        self.cap = DYNAMIC_CONFIG["capacity"][0]

        self.add_item(GameSelect([discord.SelectOption(label=g, value=g) for g in DYNAMIC_CONFIG["games"][:25]]))
        self.add_item(ModeSelect([discord.SelectOption(label=m, value=m) for m in DYNAMIC_CONFIG["modes"][:25]]))
        self.add_item(RankReqSelect([discord.SelectOption(label=r, value=r) for r in DYNAMIC_CONFIG["ranks"][:25]]))
        self.add_item(CapSelect([discord.SelectOption(label=c, value=c) for c in DYNAMIC_CONFIG["capacity"][:25]]))

    @discord.ui.button(label="🚀 この内容で募集を開始する", style=discord.ButtonStyle.success, row=4)
    async def submit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        global party_counter
        guild = interaction.guild
        user = interaction.user

        u_data = get_user_data(user.id)
        user_rank = u_data["rank"]
        if RANK_HIERARCHY[user_rank] < RANK_HIERARCHY[self.req_rank]:
            return await interaction.response.send_message(
                f"❌ あなたの登録ランク（{user_rank}）より高い条件（{self.req_rank}）は設定できません。",
                ephemeral=True
            )

        party_counter += 1
        p_id = party_counter

        # 募集人数の数値化
        max_mem = 4
        if "1人" in self.cap: max_mem = 2
        elif "2人" in self.cap: max_mem = 3
        elif "3人" in self.cap: max_mem = 4
        elif "無制限" in self.cap: max_mem = 99

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False, connect=False),
            user: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True, manage_channels=True)
        }

        category = interaction.channel.category
        text_channel = await guild.create_text_channel(name=f"pt-{p_id}-{user.name}", category=category, overwrites=overwrites)
        voice_channel = await guild.create_voice_channel(name=f"🔊 PT-{p_id} ({user.name})", category=category, user_limit=max_mem if max_mem < 99 else 0, overwrites=overwrites)

        parties[p_id] = {
            "id": p_id, "leader": user, "game": self.game, "mode": self.mode,
            "min_rank": self.req_rank, "capacity_str": self.cap, "max_members": max_mem,
            "members": [user], "text_channel": text_channel, "voice_channel": voice_channel,
            "created_at": datetime.datetime.now(datetime.timezone.utc), "notified": False
        }

        embed = discord.Embed(
            title=f"🎮 【{self.game}】パーティー作成完了 [ID: {p_id}]",
            description=f"### 📢 募集目的: **{self.mode}**\n\n"
                        f"👤 **募集者:** {user.mention} (ランク: `{user_rank}`)\n"
                        f"🎯 **条件・ランク:** `{self.req_rank}`\n"
                        f"👥 **募集人数:** `{self.cap}` (現在: 1/{max_mem if max_mem < 99 else '∞'})\n"
                        f"🔊 **専用VC:** {voice_channel.mention}\n",
            color=discord.Color.green()
        )
        await text_channel.send(content=f"{user.mention} パーティーを作成しました！", embed=embed, view=PartyControlView(p_id))
        await interaction.response.send_message(f"✅ パーティーを作成しました！ {text_channel.mention}", ephemeral=True)

class GameSelect(discord.ui.Select):
    def __init__(self, options): super().__init__(placeholder="① ゲーム名を選択...", options=options, row=0)
    async def callback(self, interaction: discord.Interaction):
        self.view.game = self.values[0]
        await interaction.response.defer()

class ModeSelect(discord.ui.Select):
    def __init__(self, options): super().__init__(placeholder="② 募集目的（モード）を選択...", options=options, row=1)
    async def callback(self, interaction: discord.Interaction):
        self.view.mode = self.values[0]
        await interaction.response.defer()

class RankReqSelect(discord.ui.Select):
    def __init__(self, options): super().__init__(placeholder="③ 参加条件ランクを選択...", options=options, row=2)
    async def callback(self, interaction: discord.Interaction):
        self.view.req_rank = self.values[0]
        await interaction.response.defer()

class CapSelect(discord.ui.Select):
    def __init__(self, options): super().__init__(placeholder="④ 募集枠数を選択...", options=options, row=3)
    async def callback(self, interaction: discord.Interaction):
        self.view.cap = self.values[0]
        await interaction.response.defer()

# --- PT管理操作（管理者削除対応） ---
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

    @discord.ui.button(label="💥 解散（管理者/リーダー権限）", style=discord.ButtonStyle.danger)
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
            description=f"{target_member.mention} さん\n{interaction.user.mention} から **{p['game']} ({p['mode']})** への招待が届きました。",
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
            "📋 募集設定を選択してください：",
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
            max_str = p['max_members'] if p['max_members'] < 99 else '∞'

            field_title = f"{status_tag} ID:{p_id} | 🎮 {p['game']}"
            field_value = (
                f"### 📢 目的: **{p['mode']}**\n"
                f"👤 **募集者:** {p['leader'].mention}\n"
                f"🎯 **条件:** `{p['min_rank']}`\n"
                f"👥 **人数:** `{len(p['members'])} / {max_str}名` ({p['capacity_str']})\n"
                f"----------------------------------------"
            )
            embed.add_field(name=field_title, value=field_value, inline=False)

            if not is_full:
                join_options.append(discord.SelectOption(
                    label=f"ID:{p_id} | {p['game']} ({p['mode']})",
                    value=str(p_id)
                ))

        view = discord.ui.View()
        if join_options:
            view.add_item(PartyJoinSelect(join_options))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, content="※現在参加可能な（満員でない）募集はありません。", ephemeral=True)

    @discord.ui.button(label="⚙️ ランク設定", style=discord.ButtonStyle.secondary, custom_id="fn_rank_set")
    async def rank_set_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("自分のフォートナイトランクを選択してください：", view=SetRankSelectView(), ephemeral=True)

    @discord.ui.button(label="👤 マイステータス", style=discord.ButtonStyle.secondary, custom_id="fn_mystats")
    async def mystats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        u_data = get_user_data(interaction.user.id)
        embed = discord.Embed(title="👤 マイステータス", color=discord.Color.blue())
        embed.add_field(name="登録ランク", value=u_data["rank"], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class PartyJoinSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="👉 参加したいパーティーを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        p = parties.get(int(self.values[0]))
        user = interaction.user
        if not p: return

        u_data = get_user_data(user.id)
        if RANK_HIERARCHY[u_data["rank"]] < RANK_HIERARCHY[p["min_rank"]]:
            return await interaction.response.send_message(f"❌ ランク条件を満たしていません (必要: {p['min_rank']} / あなた: {u_data['rank']})。", ephemeral=True)

        if user in p["members"]: return await interaction.response.send_message("すでに追加されています。", ephemeral=True)

        p["members"].append(user)
        perm = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True)
        await p["text_channel"].set_permissions(user, overwrite=perm)
        await p["voice_channel"].set_permissions(user, overwrite=perm)
        
        await p["text_channel"].send(f"🎉 {user.mention} が参加しました！")
        await interaction.response.send_message(f"参加しました！ {p['text_channel'].mention}", ephemeral=True)

# --- 管理者コマンド（選択肢の追加・削除＆管理機能） ---
@bot.tree.command(name="add_game_option", description="[管理者] PT作成時のゲーム選択肢を追加します")
@app_commands.checks.has_permissions(administrator=True)
async def add_game_option(interaction: discord.Interaction, name: str):
    if name not in DYNAMIC_CONFIG["games"]:
        DYNAMIC_CONFIG["games"].append(name)
        await interaction.response.send_message(f"✅ ゲーム選択肢に **{name}** を追加しました！", ephemeral=True)
    else:
        await interaction.response.send_message("既に存在する選択肢です。", ephemeral=True)

@bot.tree.command(name="remove_game_option", description="[管理者] PT作成時のゲーム選択肢を削除します")
@app_commands.checks.has_permissions(administrator=True)
async def remove_game_option(interaction: discord.Interaction, name: str):
    if name in DYNAMIC_CONFIG["games"]:
        DYNAMIC_CONFIG["games"].remove(name)
        await interaction.response.send_message(f"🗑️ ゲーム選択肢から **{name}** を削除しました。", ephemeral=True)
    else:
        await interaction.response.send_message("指定された選択肢が見つかりません。", ephemeral=True)

@bot.tree.command(name="add_mode_option", description="[管理者] PT作成時の募集目的（モード）選択肢を追加します")
@app_commands.checks.has_permissions(administrator=True)
async def add_mode_option(interaction: discord.Interaction, name: str):
    if name not in DYNAMIC_CONFIG["modes"]:
        DYNAMIC_CONFIG["modes"].append(name)
        await interaction.response.send_message(f"✅ 募集目的選択肢に **{name}** を追加しました！", ephemeral=True)
    else:
        await interaction.response.send_message("既に存在する選択肢です。", ephemeral=True)

@bot.tree.command(name="remove_mode_option", description="[管理者] PT作成時の募集目的（モード）選択肢を削除します")
@app_commands.checks.has_permissions(administrator=True)
async def remove_mode_option(interaction: discord.Interaction, name: str):
    if name in DYNAMIC_CONFIG["modes"]:
        DYNAMIC_CONFIG["modes"].remove(name)
        await interaction.response.send_message(f"🗑️ 募集目的選択肢から **{name}** を削除しました。", ephemeral=True)
    else:
        await interaction.response.send_message("指定された選択肢が見つかりません。", ephemeral=True)

@bot.tree.command(name="setup_log_channel", description="[管理者] サーバー入退室通知の送信先チャンネルを設定します")
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
        description="以下のボタンからパーティーの作成・確認・参加ができます！\n\n"
                    "🎮 **PT作成:** 選択肢から選ぶだけで素早く募集開始\n"
                    "📋 **PT一覧・参加:** 現在募集中のパーティーを確認・参加\n"
                    "⚙️ **ランク設定:** 自分の最高ランクを登録\n"
                    "👤 **マイステータス:** 登録情報の確認",
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
