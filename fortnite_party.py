import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import random
import aiohttp
import os  # ← 環境変数読み込み用に追加

# --- Botの初期設定 ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # サーバー入退室監視・メンバー検索に必須
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------
# ⚙️ 管理者設定：ゲームルール（モード）の追加・削除はここを編集！
# ---------------------------------------------------------
GAME_MODES = [
    "バトルロイヤル ランク",
    "ゼロビルド ランク",
    "アンランク / エンジョイ",
    "クリエイティブ / 練習",
    "大会 / スクエイム"
]

# ランク選択肢と順序
RANK_OPTIONS = [
    "制限なし",
    "ブロンズ以上",
    "シルバー以上",
    "ゴールド以上",
    "プラチナ以上",
    "ダイヤモンド以上",
    "エリート以上",
    "チャンピオン以上",
    "アンリアル以上"
]

RANK_HIERARCHY = {
    "制限なし": 0, "ブロンズ": 1, "シルバー": 2, "ゴールド": 3,
    "プラチナ": 4, "ダイヤモンド": 5, "エリート": 6, "チャンピオン": 7, "アンリアル": 8,
    "ブロンズ以上": 1, "シルバー以上": 2, "ゴールド以上": 3,
    "プラチナ以上": 4, "ダイヤモンド以上": 5, "エリート以上": 6, "チャンピオン以上": 7, "アンリアル以上": 8
}

# データ保持用
user_link_data = {}
parties = {}
party_counter = 0
giveaways = {}
giveaway_counter = 0

# 招待クールダウン管理データ ( { (party_id, target_user_id): timestamp } )
invite_cooldowns = {}

# ログ送信先チャンネルID
log_channel_id = None

def get_user_data(user_id):
    if user_id not in user_link_data:
        user_link_data[user_id] = {
            "epic_id": "未連携",
            "rank": "制限なし"
        }
    return user_link_data[user_id]

# ログ送信共通関数（サーバー入退通知など）
async def send_log(guild: discord.Guild, embed: discord.Embed):
    global log_channel_id
    if log_channel_id:
        ch = guild.get_channel(log_channel_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

# --- Fortnite APIを使ってランクを自動取得する関数 ---
async def fetch_fortnite_rank(epic_name: str) -> str:
    url = f"https://fortnite-api.com/v2/stats/br/v2?name={epic_name}"
    headers = {"Authorization": "ここにAPIキーを入力してください"}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    return "ダイヤモンド" # 取得成功時の仮置き
                else:
                    return None
        except Exception:
            return None

# ---------------------------------------------------------
# 1. サーバー参加・退出の通知イベント
# ---------------------------------------------------------
@bot.event
async def on_member_join(member: discord.Member):
    embed = discord.Embed(
        title="📥 メンバー参加",
        description=f"{member.mention} (`{member.name}`) がサーバーに参加しました！",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="アカウント作成日", value=discord.utils.format_dt(member.created_at, style="R"), inline=True)
    await send_log(member.guild, embed)

@bot.event
async def on_member_remove(member: discord.Member):
    embed = discord.Embed(
        title="📤 メンバー退出",
        description=f"{member.mention} (`{member.name}`) がサーバーから退出しました。",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    await send_log(member.guild, embed)

# ---------------------------------------------------------
# 2. Epicアカウント連携 Modal
# ---------------------------------------------------------
class LinkEpicModal(discord.ui.Modal, title='Epic Games アカウント連携'):
    epic_id = discord.ui.TextInput(label='Epicアカウント名', placeholder='例: FortniteGamer123')

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        epic_name = self.epic_id.value.strip()

        fetched_rank = await fetch_fortnite_rank(epic_name)

        if not fetched_rank:
            return await interaction.followup.send(
                "❌ Epicアカウントが見つからないか、戦績が非公開になっています。\n"
                "フォートナイトのゲーム内設定で「キャリアリーダーボードに表示」をONにしてください。",
                ephemeral=True
            )

        u_data = get_user_data(user.id)
        u_data["epic_id"] = epic_name
        u_data["rank"] = fetched_rank

        embed = discord.Embed(title="🔗 アカウント連携完了 (自動取得)", color=discord.Color.green())
        embed.add_field(name="Epic ID", value=epic_name, inline=False)
        embed.add_field(name="自動取得された最高ランク", value=fetched_rank, inline=False)
        embed.set_footer(text="※この情報はあなただけに公開されています。")

        await interaction.followup.send(embed=embed, ephemeral=True)

# ---------------------------------------------------------
# 3. パーティー作成（選択メニュー＆詳細設定）
# ---------------------------------------------------------
class CreatePartySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.selected_mode = None
        self.selected_rank = None
        self.selected_privacy = "公開募集"

        mode_options = [discord.SelectOption(label=m, value=m) for m in GAME_MODES]
        self.add_item(ModeSelect(mode_options))

        rank_options = [discord.SelectOption(label=r, value=r) for r in RANK_OPTIONS]
        self.add_item(RankSelect(rank_options))

        privacy_options = [
            discord.SelectOption(label="🌐 公開募集（誰でも参加可）", value="公開募集", description="PT一覧パネルに表示されます"),
            discord.SelectOption(label="🔒 招待限定（リーダーの招待のみ）", value="招待限定", description="PT一覧パネルに表示されません")
        ]
        self.add_item(PrivacySelect(privacy_options))

    @discord.ui.button(label="📝 詳細条件を入力して作成", style=discord.ButtonStyle.green, row=3)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_mode or not self.selected_rank:
            return await interaction.response.send_message("❌ 「ゲームルール」と「条件ランク」の両方を選択してください！", ephemeral=True)

        await interaction.response.send_modal(
            PartyDetailModal(self.selected_mode, self.selected_rank, self.selected_privacy)
        )

class ModeSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="① ゲームルール（モード）を選択...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_mode = self.values[0]
        await interaction.response.send_message(f"✅ ルール: **{self.values[0]}** を選択しました。", ephemeral=True)

class RankSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="② 参加条件ランクを選択...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_rank = self.values[0]
        await interaction.response.send_message(f"✅ 条件ランク: **{self.values[0]}** を選択しました。", ephemeral=True)

class PrivacySelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="③ 募集タイプを選択（公開 / 招待限定）...", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_privacy = self.values[0]
        await interaction.response.send_message(f"✅ 募集タイプ: **{self.values[0]}** を選択しました。", ephemeral=True)

class PartyDetailModal(discord.ui.Modal, title='パーティー詳細設定'):
    def __init__(self, mode, min_rank, privacy):
        super().__init__()
        self.mode = mode
        self.min_rank = min_rank
        self.privacy = privacy

    start_time = discord.ui.TextInput(label='開始時間', placeholder='例: 21:00〜 / 今すぐ！', max_length=30)
    conditions = discord.ui.TextInput(label='詳細条件', placeholder='例: 聞き専OK, エンジョイ勢', style=discord.TextStyle.paragraph, max_length=200, required=False)
    hashtags = discord.ui.TextInput(label='ハッシュタグ', placeholder='例: #ゼロビルド #初心者歓迎', max_length=50, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        global party_counter
        guild = interaction.guild
        user = interaction.user

        u_data = get_user_data(user.id)
        if u_data["epic_id"] == "未連携":
            return await interaction.response.send_message("❌ パネルの「🔗 Epic連携」ボタンを押してアカウント連携を行ってください！", ephemeral=True)

        user_rank = u_data["rank"]
        if RANK_HIERARCHY[user_rank] < RANK_HIERARCHY[self.min_rank]:
            return await interaction.response.send_message(f"❌ あなた自身のランク（{user_rank}）より高い参加条件（{self.min_rank}）は設定できません！", ephemeral=True)

        party_counter += 1
        p_id = party_counter

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False, connect=False),
            user: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True, connect=True, speak=True, manage_channels=True)
        }

        category = interaction.channel.category 
        text_channel = await guild.create_text_channel(name=f"fn-pt-{user.name}", category=category, overwrites=overwrites)
        voice_channel = await guild.create_voice_channel(name=f"🔊 FN-PT ({user.name})", category=category, user_limit=4, overwrites=overwrites)

        parties[p_id] = {
            "id": p_id, "leader": user, "mode": self.mode, "start_time": self.start_time.value,
            "min_rank": self.min_rank, "privacy": self.privacy,
            "conditions": self.conditions.value or "なし", "hashtags": self.hashtags.value or "#フォートナイト",
            "members": [user], "text_channel": text_channel, "voice_channel": voice_channel,
            "created_at": discord.utils.utcnow(), "notified": False 
        }

        privacy_icon = "🌐 公開" if self.privacy == "公開募集" else "🔒 招待限定"

        embed = discord.Embed(
            title=f"🎮 フォートナイトパーティー [ID: {p_id}] ({privacy_icon})",
            description=f"**モード:** {self.mode}\n**リーダー:** {user.mention} (ランク: {user_rank})\n"
                        f"**開始時間:** {self.start_time.value}\n**条件ランク:** {self.min_rank}\n"
                        f"**詳細条件:** {parties[p_id]['conditions']}\n**タグ:** {parties[p_id]['hashtags']}\n\n"
                        f"**参加メンバー:** {user.mention} (1/4)\n🔊 **専用VC:** {voice_channel.mention}",
            color=discord.Color.blue() if self.privacy == "公開募集" else discord.Color.purple()
        )
        await text_channel.send(content=f"{user.mention} パーティーが作成されました！", embed=embed, view=PartyControlView(p_id))
        await interaction.response.send_message(f"パーティーを作成しました！ ({privacy_icon}) {text_channel.mention}", ephemeral=True)

class PartyControlView(discord.ui.View):
    def __init__(self, party_id):
        super().__init__(timeout=None)
        self.party_id = party_id

    @discord.ui.button(label="✉️ メンバーを招待", style=discord.ButtonStyle.primary)
    async def invite_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p or interaction.user != p["leader"]:
            return await interaction.response.send_message("権限がありません（リーダーのみ招待可能です）。", ephemeral=True)
        if len(p["members"]) >= 4:
            return await interaction.response.send_message("パーティーは既に満員です。", ephemeral=True)

        guild_members = [m for m in interaction.guild.members if not m.bot and m not in p["members"]]
        if not guild_members:
            return await interaction.response.send_message("招待できるメンバーがいません。", ephemeral=True)

        view = discord.ui.View()
        view.add_item(AllMemberInviteSelect(self.party_id, guild_members))
        await interaction.response.send_message("招待するサーバーメンバーを選択してください（オフライン含む）:", view=view, ephemeral=True)

    @discord.ui.button(label="👑 リーダー譲渡", style=discord.ButtonStyle.secondary)
    async def transfer_leader(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p or interaction.user != p["leader"]:
            return await interaction.response.send_message("権限がありません。", ephemeral=True)
        other_members = [m for m in p["members"] if m != p["leader"]]
        if not other_members:
            return await interaction.response.send_message("譲渡できる他メンバーがいません。", ephemeral=True)
        view = discord.ui.View()
        view.add_item(MemberSelect(self.party_id, other_members))
        await interaction.response.send_message("新リーダーを選択:", view=view, ephemeral=True)

    @discord.ui.button(label="🚪 パーティー脱退", style=discord.ButtonStyle.secondary, custom_id="party_leave")
    async def leave_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p:
            return await interaction.response.send_message("このパーティーは既に存在しません。", ephemeral=True)
        
        user = interaction.user
        if user not in p["members"]:
            return await interaction.response.send_message("あなたはこのパーティーに参加していません。", ephemeral=True)
        
        if user == p["leader"]:
            return await interaction.response.send_message("リーダーは脱退できません。「解散（削除）」を行うか、先に「リーダー権限譲渡」を行ってください。", ephemeral=True)

        p["members"].remove(user)

        try:
            await p["text_channel"].set_permissions(user, overwrite=None)
            await p["voice_channel"].set_permissions(user, overwrite=None)
        except Exception:
            pass

        await p["text_channel"].send(f"🚪 {user.mention} がパーティーから脱退しました。 ({len(p['members'])}/4名)")
        await interaction.response.send_message("パーティーから脱退しました。", ephemeral=True)

    @discord.ui.button(label="💥 解散（削除）", style=discord.ButtonStyle.danger)
    async def delete_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p or interaction.user != p["leader"]:
            return await interaction.response.send_message("権限がありません。", ephemeral=True)
        await interaction.response.send_message("解散中...")
        await asyncio.sleep(2)

        try:
            await p["text_channel"].delete()
            await p["voice_channel"].delete()
        except Exception:
            pass
        parties.pop(self.party_id, None)

class AllMemberInviteSelect(discord.ui.Select):
    def __init__(self, party_id, members):
        self.party_id = party_id
        options = [
            discord.SelectOption(
                label=m.display_name, 
                value=str(m.id), 
                description=f"@{m.name} ({'オンライン' if m.status != discord.Status.offline else 'オフライン'})"
            ) for m in members[:25]
        ]
        super().__init__(placeholder="招待するメンバーを選択 (最大25名表示)...", options=options)

    async def callback(self, interaction: discord.Interaction):
        p = parties.get(self.party_id)
        if not p: return

        target_id = int(self.values[0])
        target_member = interaction.guild.get_member(target_id)
        if not target_member:
            return await interaction.response.send_message("メンバーが見つかりませんでした。", ephemeral=True)

        now = datetime.datetime.utcnow().timestamp()
        key = (self.party_id, target_id)
        last_invited = invite_cooldowns.get(key, 0)
        
        if now - last_invited < 900:
            remaining = int((900 - (now - last_invited)) // 60)
            return await interaction.response.send_message(
                f"⏱️ {target_member.mention} さんへの再招待は、あと **{remaining+1} 分間** 行えません。（クールダウン制限: 15分）",
                ephemeral=True
            )

        invite_cooldowns[key] = now

        view = PartyInviteAcceptView(self.party_id, target_member.id)
        embed = discord.Embed(
            title="✉️ パーティー招待が届きました！",
            description=f"{target_member.mention} さん\n"
                        f"{interaction.user.mention} からフォートナイトパーティーへの招待が届きました。\n\n"
                        f"**ゲームルール:** {p['mode']}\n"
                        f"**参加条件:** {p['min_rank']}",
            color=discord.Color.gold()
        )
        
        await p["text_channel"].send(content=f"{target_member.mention}", embed=embed, view=view)
        await interaction.response.send_message(f"✅ {target_member.mention} に招待通知を送りました（相手が承認すると参加になります）。", ephemeral=True)

class PartyInviteAcceptView(discord.ui.View):
    def __init__(self, party_id, target_user_id):
        super().__init__(timeout=None)
        self.party_id = party_id
        self.target_user_id = target_user_id

    @discord.ui.button(label="✅ 参加する（承認）", style=discord.ButtonStyle.success)
    async def accept_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = parties.get(self.party_id)
        if not p:
            return await interaction.response.send_message("このパーティーは既に解散または存在しません。", ephemeral=True)

        user = interaction.user
        if user.id != self.target_user_id:
            return await interaction.response.send_message("あなた宛ての招待ではありません。", ephemeral=True)

        if len(p["members"]) >= 4:
            return await interaction.response.send_message("❌ パーティーは既に満員です。", ephemeral=True)

        if user in p["members"]:
            return await interaction.response.send_message("既にパーティーに参加しています。", ephemeral=True)

        u_data = get_user_data(user.id)
        if u_data["epic_id"] == "未連携":
            return await interaction.response.send_message("❌ パネルの「🔗 Epic連携」ボタンを押して先にアカウント連携を行ってください！", ephemeral=True)

        p["members"].append(user)

        permission_override = discord.PermissionOverwrite(
            read_messages=True, 
            view_channel=True, 
            send_messages=True, 
            connect=True, 
            speak=True
        )
        await p["text_channel"].set_permissions(user, overwrite=permission_override)
        await p["voice_channel"].set_permissions(user, overwrite=permission_override)

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        await p["text_channel"].send(f"🎉 {user.mention} が招待を承認して参加しました！ ({len(p['members'])}/4名)")
        await interaction.response.send_message(f"パーティーに参加しました！ {p['text_channel'].mention}", ephemeral=True)

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
            await p["text_channel"].send(f"👑 {new_l.mention} が新しいリーダーになりました！")

# ---------------------------------------------------------
# 4. 無料配布（抽選） ＆ サポートチケット
# ---------------------------------------------------------
class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="🎉 抽選に応募する", style=discord.ButtonStyle.success, custom_id="join_giveaway")
    async def join_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = giveaways.get(self.giveaway_id)
        if not g:
            return await interaction.response.send_message("この配布は終了しています。", ephemeral=True)

        user = interaction.user
        g["entries"][user.id] = 1

        await interaction.response.send_message("✅ 抽選に応募しました！", ephemeral=True)

async def create_support_ticket(guild: discord.Guild, winner: discord.Member, reward_title: str):
    category = discord.utils.get(guild.categories, name="お問い合わせ")
    if not category:
        category = await guild.create_category("お問い合わせ")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
        winner: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
    }

    ticket_ch = await guild.create_text_channel(
        name=f"🎁受け取り-{winner.name}",
        category=category,
        overwrites=overwrites
    )

    embed = discord.Embed(
        title="🎁 無料配布 当選受け取り窓口",
        description=f"{winner.mention} さん、ご当選おめでとうございます！🎉\n\n"
                    f"**当選賞品:** {reward_title}\n"
                    f"スタッフが対応するまで、このチャンネルでお待ちください。",
        color=discord.Color.gold()
    )
    await ticket_ch.send(content=f"{winner.mention}", embed=embed)
    return ticket_ch

# ---------------------------------------------------------
# 5. メインパネル
# ---------------------------------------------------------
class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎮 PT作成", style=discord.ButtonStyle.green, custom_id="fn_create")
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🎮 作成するパーティーの設定を選択してください：",
            view=CreatePartySelectView(),
            ephemeral=True
        )

    @discord.ui.button(label="📋 PT一覧・参加", style=discord.ButtonStyle.primary, custom_id="fn_list")
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        public_parties = {p_id: p for p_id, p in parties.items() if p.get("privacy", "公開募集") == "公開募集"}

        if not public_parties:
            return await interaction.response.send_message("現在募集中の公開PTはありません。", ephemeral=True)

        embed = discord.Embed(title="🏆 募集中のフォートナイトPT", color=discord.Color.gold())
        for p_id, p in list(public_parties.items()):
            member_names = [
                f"{m.display_name} (👑)" if m == p["leader"] else m.display_name 
                for m in p["members"]
            ]
            members_str = ", ".join(member_names)

            embed.add_field(
                name=f"ID:{p_id} | {p['mode']} ({len(p['members'])}/4名)",
                value=f"・**リーダー:** {p['leader'].mention}\n"
                      f"・**メンバー:** {members_str}\n"
                      f"・**条件:** {p['min_rank']}\n"
                      f"・**開始:** {p['start_time']}",
                inline=False
            )

        view = discord.ui.View()
        options = [discord.SelectOption(label=f"ID:{p_id} - {p['mode']}", value=str(p_id)) for p_id, p in public_parties.items() if len(p['members']) < 4]
        if options:
            view.add_item(PartyJoinSelect(options))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, content="満員でない公開PTはありません。", ephemeral=True)

    @discord.ui.button(label="🔗 Epic連携", style=discord.ButtonStyle.secondary, custom_id="fn_link")
    async def link_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LinkEpicModal())

    @discord.ui.button(label="👤 マイステータス", style=discord.ButtonStyle.secondary, custom_id="fn_mystats")
    async def mystats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        u_data = get_user_data(interaction.user.id)
        embed = discord.Embed(title="👤 マイステータス", color=discord.Color.blue())
        embed.add_field(name="Epic ID", value=u_data["epic_id"], inline=False)
        embed.add_field(name="登録ランク", value=u_data["rank"], inline=False)
        embed.set_footer(text="※PT作成や参加にはEpic連携が必要です。")
        await interaction.response.send_message(embed=embed, ephemeral=True)

class PartyJoinSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="参加するPTを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        p = parties.get(int(self.values[0]))
        user = interaction.user
        if not p: return

        u_data = get_user_data(user.id)
        if u_data["epic_id"] == "未連携":
            return await interaction.response.send_message("❌ 先に「🔗 Epic連携」ボタンを押して連携してください。", ephemeral=True)

        if RANK_HIERARCHY[u_data["rank"]] < RANK_HIERARCHY[p["min_rank"]]:
            return await interaction.response.send_message(f"❌ ランク条件を満たしていません (必要: {p['min_rank']})。", ephemeral=True)

        if user in p["members"]: return await interaction.response.send_message("参加済みです。", ephemeral=True)

        p["members"].append(user)
        
        permission_override = discord.PermissionOverwrite(
            read_messages=True, 
            view_channel=True, 
            send_messages=True, 
            connect=True, 
            speak=True
        )
        await p["text_channel"].set_permissions(user, overwrite=permission_override)
        await p["voice_channel"].set_permissions(user, overwrite=permission_override)
        
        await p["text_channel"].send(f"🎉 {user.mention} が参加しました！ ({len(p['members'])}/4名)")
        await interaction.response.send_message(f"参加しました！ {p['text_channel'].mention}", ephemeral=True)

# --- スラッシュコマンド群（管理者用） ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    check_party_expiration.start()
    await bot.tree.sync()

@bot.tree.command(name="setup_log_channel", description="[管理者] サーバー入退室通知の送信先チャンネルを設定します")
@app_commands.checks.has_permissions(administrator=True)
async def setup_log_channel(interaction: discord.Interaction):
    global log_channel_id
    log_channel_id = interaction.channel_id
    await interaction.response.send_message(f"✅ このチャンネル ({interaction.channel.mention}) をサーバー入退室通知の送信先に設定しました！", ephemeral=True)

@bot.tree.command(name="create_giveaway", description="[管理者] 無料配布パネルの作成")
@app_commands.checks.has_permissions(administrator=True)
async def create_giveaway(interaction: discord.Interaction, title: str, winners_count: int = 1):
    global giveaway_counter
    giveaway_counter += 1
    g_id = giveaway_counter
    giveaways[g_id] = {"title": title, "winners_count": winners_count, "entries": {}, "channel_id": interaction.channel_id}

    embed = discord.Embed(
        title=f"🎁 無料配布: {title}",
        description=f"下のボタンを押して抽選に応募しよう！\n**当選人数:** {winners_count}名",
        color=discord.Color.gold()
    )
    await interaction.channel.send(embed=embed, view=GiveawayView(g_id))
    await interaction.response.send_message("配布パネルを作成しました。", ephemeral=True)

@bot.tree.command(name="reroll_giveaway", description="[管理者] 無料配布の抽選を実行")
@app_commands.checks.has_permissions(administrator=True)
async def reroll_giveaway(interaction: discord.Interaction, giveaway_id: int):
    g = giveaways.get(giveaway_id)
    if not g or not g["entries"]:
        return await interaction.response.send_message("応募者が見つかりません。", ephemeral=True)

    user_pool = list(g["entries"].keys())
    winners = []
    k = min(g["winners_count"], len(user_pool))
    while len(winners) < k:
        chosen = random.choice(user_pool)
        if chosen not in winners:
            winners.append(chosen)

    await interaction.response.send_message(f"🎉 抽選完了！ 当選者をチケットへ案内しています...", ephemeral=True)

    for w_id in winners:
        member = interaction.guild.get_member(w_id)
        if member:
            ch = await create_support_ticket(interaction.guild, member, g["title"])
            await interaction.channel.send(f"🎉 {member.mention} さんが **{g['title']}** に当選しました！ お問い合わせチケットを作成しました: {ch.mention}")

@bot.tree.command(name="setup_fortnite_panel", description="[管理者] 募集パネル設置")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ フォートナイト パーティー募集＆総合メニュー",
        description="各機能は以下のボタンを押すだけで利用できます！\n\n"
                    "🎮 **PT作成:** パーティー募集を開始します（公開 / 招待限定が選択可）\n"
                    "📋 **PT一覧・参加:** 現在募集中の公開パーティーを確認・参加します\n"
                    "🔗 **Epic連携:** Epic IDを入力して最新ランクを自動取得します\n"
                    "👤 **マイステータス:** 自分の連携状況やランクを確認します",
        color=discord.Color.dark_purple()
    )
    await interaction.channel.send(embed=embed, view=MainPanelView())
    await interaction.response.send_message("設置完了。", ephemeral=True)

# 48時間/50時間チェックタスク
@tasks.loop(minutes=5)
async def check_party_expiration():
    now = datetime.datetime.utcnow()
    for p_id, p in list(parties.items()):
        elapsed = now - p["created_at"].replace(tzinfo=None)
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

# --- Renderなどの環境変数からトークンを安全に読み込んで起動 ---
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    # ローカル（PC）で直接テスト実行する場合用のフォールバック
    TOKEN = "ここにローカルテスト用のトークンを直接書いても動きます"

bot.run(TOKEN)
