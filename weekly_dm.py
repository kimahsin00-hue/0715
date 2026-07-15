"""
매주 반복 공지 cog.

기존 status_report.py 와는 별개의 기능입니다:
- 공지를 여러 개 등록할 수 있고, 공지마다 요일/시간/내용을 각각 따로 지정합니다.
  (기존처럼 "일정 하나에 여러 공지 텍스트를 몰아넣는" 방식이 아닙니다)
- 등록된 공지는 목록에서 확인하고, 개별적으로 삭제할 수 있습니다.
- 지정한 요일/시간이 되면, 해당 서버(길드)의 전체 인원에게 "개인 DM"으로 발송됩니다.
  (채널에 올리는 방식이 아닙니다)

주의(실행 전 확인 필요):
1) 이 cog가 정상 동작하려면 봇에 "서버 멤버 목록 인텐트(Members Intent)"가
   Discord 개발자 포털 + 코드(intents.members = True) 양쪽에 켜져 있어야 합니다.
   꺼져 있으면 guild.members 가 봇/자기 자신만 보이거나 비어 있을 수 있습니다.
2) DB는 기존 프로젝트의 get_db() (config.KST 사용)를 그대로 재사용했고,
   테이블은 이 파일에서 최초 실행 시 자동 생성됩니다(CREATE TABLE IF NOT EXISTS).
   기존 db.py를 따로 건드릴 필요는 없습니다.
3) 여러 서버(길드)에서 동시에 쓰는 봇이라 가정하고, 공지는 "등록한 채널이 속한 길드" 기준으로
   그 길드의 전체 인원에게만 DM이 갑니다. 다른 길드에는 영향 없습니다.
4) 같은 분(minute)에 정확히 매칭될 때 발송하는 방식이라, 60초 주기 루프가 밀리는 상황(봇 랙 등)을
   고려해 "이미 오늘 보낸 공지인지"를 last_sent_date로 체크해서 중복 발송을 막았습니다.
"""
import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import KST
from db import get_db

WEEKDAY_CHOICES = [
    app_commands.Choice(name="매주 월요일", value=0),
    app_commands.Choice(name="매주 화요일", value=1),
    app_commands.Choice(name="매주 수요일", value=2),
    app_commands.Choice(name="매주 목요일", value=3),
    app_commands.Choice(name="매주 금요일", value=4),
    app_commands.Choice(name="매주 토요일", value=5),
    app_commands.Choice(name="매주 일요일", value=6),
]
WEEKDAY_LABEL = {c.value: c.name for c in WEEKDAY_CHOICES}


# ==========================================
# [DB 헬퍼]
# ==========================================
def _ensure_table():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            weekday INTEGER NOT NULL,     -- 0=월 ... 6=일
            hour INTEGER NOT NULL,        -- 0-23
            minute INTEGER NOT NULL,      -- 0-59
            content TEXT NOT NULL,
            created_by INTEGER,
            last_sent_date TEXT           -- 'YYYY-MM-DD', 같은 날 중복 발송 방지용
        )
        """
    )
    conn.commit()


def _add_notice(guild_id, weekday, hour, minute, content, created_by):
    conn = get_db()
    conn.execute(
        "INSERT INTO scheduled_notices (guild_id, weekday, hour, minute, content, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, weekday, hour, minute, content, created_by),
    )
    conn.commit()


def _get_notices(guild_id):
    conn = get_db()
    return conn.execute(
        "SELECT id, weekday, hour, minute, content FROM scheduled_notices WHERE guild_id=? ORDER BY weekday, hour, minute",
        (guild_id,),
    ).fetchall()


def _delete_notice(notice_id: int, guild_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM scheduled_notices WHERE id=? AND guild_id=?", (notice_id, guild_id))
    conn.commit()
    return cur.rowcount > 0


def _get_due_notices(weekday, hour, minute, today_str):
    conn = get_db()
    return conn.execute(
        "SELECT id, guild_id, content FROM scheduled_notices "
        "WHERE weekday=? AND hour=? AND minute=? AND (last_sent_date IS NULL OR last_sent_date != ?)",
        (weekday, hour, minute, today_str),
    ).fetchall()


def _mark_sent(notice_id, today_str):
    conn = get_db()
    conn.execute("UPDATE scheduled_notices SET last_sent_date=? WHERE id=?", (today_str, notice_id))
    conn.commit()


# ==========================================
# [공지 등록 모달]
# ==========================================
class NoticeContentModal(discord.ui.Modal, title="공지 내용 입력"):
    content_input = discord.ui.TextInput(
        label="공지 내용",
        style=discord.TextStyle.paragraph,
        placeholder="전체 인원에게 개인 DM으로 발송될 내용을 입력하세요.",
        required=True,
        max_length=1500,
    )

    def __init__(self, guild_id, weekday, hour, minute, created_by):
        super().__init__()
        self.guild_id, self.weekday, self.hour, self.minute, self.created_by = (
            guild_id, weekday, hour, minute, created_by,
        )

    async def on_submit(self, interaction: discord.Interaction):
        _add_notice(self.guild_id, self.weekday, self.hour, self.minute, str(self.content_input.value), self.created_by)
        label = WEEKDAY_LABEL[self.weekday]
        await interaction.response.send_message(
            f"✅ 공지가 등록되었습니다.\n**{label} {self.hour:02d}:{self.minute:02d}** 에 전체 인원에게 DM으로 발송됩니다.",
            ephemeral=True,
        )


# ==========================================
# [공지 삭제 선택 UI]
# ==========================================
class NoticeDeleteSelect(discord.ui.Select):
    def __init__(self, rows, guild_id):
        self.guild_id = guild_id
        options = []
        for nid, weekday, hour, minute, content in rows[:25]:
            label = f"{WEEKDAY_LABEL[weekday]} {hour:02d}:{minute:02d}"
            desc = (content[:90] if content else "")
            options.append(discord.SelectOption(label=label, value=str(nid), description=desc))
        super().__init__(placeholder="삭제할 공지를 선택하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        notice_id = int(self.values[0])
        ok = _delete_notice(notice_id, self.guild_id)
        if ok:
            await interaction.response.send_message("🗑️ 공지를 삭제했습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("삭제에 실패했습니다. (이미 삭제되었을 수 있습니다)", ephemeral=True)


class NoticeDeleteView(discord.ui.View):
    def __init__(self, rows, guild_id):
        super().__init__(timeout=60)
        self.add_item(NoticeDeleteSelect(rows, guild_id))


# ==========================================
# [Cog / 슬래시 커맨드 / 발송 루프]
# ==========================================
class NoticeScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_table()
        self.notice_loop.start()

    def cog_unload(self):
        self.notice_loop.cancel()

    @app_commands.command(name="공지등록", description="[관리자] 매주 반복되는 공지를 등록합니다 (요일/시간/내용 각각 지정)")
    @app_commands.describe(요일="공지가 발송될 요일", 시="발송 시각(시, 0-23)", 분="발송 시각(분, 0-59)")
    @app_commands.choices(요일=WEEKDAY_CHOICES)
    @app_commands.default_permissions(administrator=True)
    async def register_notice(self, interaction: discord.Interaction, 요일: app_commands.Choice[int], 시: app_commands.Range[int, 0, 23], 분: app_commands.Range[int, 0, 59]):
        await interaction.response.send_modal(
            NoticeContentModal(interaction.guild_id, 요일.value, 시, 분, interaction.user.id)
        )

    @app_commands.command(name="공지목록", description="[관리자] 등록된 반복 공지 목록을 확인합니다")
    @app_commands.default_permissions(administrator=True)
    async def list_notices(self, interaction: discord.Interaction):
        rows = _get_notices(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("등록된 공지가 없습니다.", ephemeral=True)
            return
        lines = []
        for nid, weekday, hour, minute, content in rows:
            preview = content if len(content) <= 60 else content[:60] + "…"
            lines.append(f"`#{nid}` **{WEEKDAY_LABEL[weekday]} {hour:02d}:{minute:02d}** — {preview}")
        embed = discord.Embed(title="등록된 반복 공지", description="\n".join(lines), color=0x2b2d31)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="공지삭제", description="[관리자] 등록된 반복 공지를 삭제합니다")
    @app_commands.default_permissions(administrator=True)
    async def delete_notice(self, interaction: discord.Interaction):
        rows = _get_notices(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("삭제할 공지가 없습니다.", ephemeral=True)
            return
        embed = discord.Embed(title="공지 삭제", description="삭제할 공지를 선택해주세요.", color=0xe74c3c)
        await interaction.response.send_message(embed=embed, view=NoticeDeleteView(rows, interaction.guild_id), ephemeral=True)

    @tasks.loop(seconds=60)
    async def notice_loop(self):
        now = datetime.now(KST)
        today_str = now.strftime('%Y-%m-%d')
        due = _get_due_notices(now.weekday(), now.hour, now.minute, today_str)
        for notice_id, guild_id, content in due:
            _mark_sent(notice_id, today_str)  # 먼저 마킹: 발송 중 루프가 다시 돌아도 중복 방지
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            embed = discord.Embed(title="📢 공지", description=content, color=0x3498db)
            for member in guild.members:
                if member.bot:
                    continue
                try:
                    await member.send(embed=embed)
                except Exception:
                    pass
                await asyncio.sleep(0.5)  # DM 레이트리밋 완화

    @notice_loop.before_loop
    async def before_notice_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(NoticeScheduleCog(bot))
