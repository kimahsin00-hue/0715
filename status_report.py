"""
축복/에다니아 제보 cog.

원본에 있던 두 가지 기능은 사용자와 상의해서 다음과 같이 정리했습니다:
- status_alert_users (신규 제보 DM 구독): 값을 넣는 명령어가 원본에 없어서 지금까지
  한 번도 작동한 적이 없었음. 이번에 안 살리기로 함 — 코드는 그대로 두되(해로울 게
  없어서) 구독 명령어는 추가하지 않았습니다. 즉 여전히 아무도 DM을 못 받습니다.
- status_notify_channels (제보를 특정 채널로 몰아보내기): 이번에 /제보채널지정
  명령어를 새로 추가해서 살렸습니다.

원본에는 _get_status_rows 함수가 파일 안에 두 번 정의되어 있었는데(완전히 동일한 로직),
여기서는 하나로 합쳤습니다.
"""
import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from config import KST
from db import get_db, save_panel
from utils import schedule_ephemeral_delete
from data.status_data import (
    STATUS_KIND_OPTIONS, STATUS_SERVER_REGIONS, STATUS_SERVER_NUMBERS,
    STATUS_DEFAULT_MINUTES, STATUS_TITLES,
)


# ==========================================
# [DB 헬퍼]
# ==========================================
def _get_status_rows(s_type):
    conn = get_db()
    cutoff = (datetime.now(KST) - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        "SELECT content, timestamp, expire_time FROM status_reports WHERE type=? AND (expire_time IS NULL OR expire_time >= ?) ORDER BY timestamp DESC LIMIT 8",
        (s_type, cutoff),
    ).fetchall()
    return rows


def _get_user_status_rows(s_type, user_id):
    """특정 유저 본인이 등록한, 아직 만료되지 않은 제보 목록."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, content, timestamp FROM status_reports WHERE type=? AND user_id=? AND (expire_time IS NULL OR expire_time >= ?) ORDER BY timestamp DESC LIMIT 20",
        (s_type, user_id, datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')),
    ).fetchall()
    return rows


def _delete_status_report(report_id: int):
    conn = get_db()
    conn.execute("DELETE FROM status_reports WHERE id=?", (report_id,))
    conn.commit()


async def get_status_notify_channel(bot: commands.Bot, s_type, fallback_channel):
    conn = get_db()
    row = conn.execute("SELECT channel_id FROM status_notify_channels WHERE s_type=?", (s_type,)).fetchone()
    if row:
        channel = bot.get_channel(row[0])
        if channel:
            return channel
    return fallback_channel


# ==========================================
# [제보 등록 체인: 종류 → (지역 → 서버번호) → 시간]
# ==========================================
class StatusManualTimeModal(discord.ui.Modal, title="시간 직접 입력"):
    hour_input = discord.ui.TextInput(label='남은 시간 (시)', placeholder='예: 1  (없으면 0)', required=True, max_length=2)
    minute_input = discord.ui.TextInput(label='남은 시간 (분)', placeholder='예: 30  (없으면 0)', required=True, max_length=2)

    def __init__(self, bot: commands.Bot, s_type, kind, server):
        super().__init__()
        self.bot, self.s_type, self.kind, self.server = bot, s_type, kind, server

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hours = int(self.hour_input.value)
            mins = int(self.minute_input.value)
            total_minutes = hours * 60 + mins
            if total_minutes <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("시간과 분을 올바른 숫자로 입력해주세요. (합산 1분 이상)", ephemeral=True)
            return
        await finalize_status_report(self.bot, interaction, self.s_type, self.kind, self.server, total_minutes)


class StatusTimeSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, s_type, kind, server):
        super().__init__(timeout=120)
        self.bot, self.s_type, self.kind, self.server = bot, s_type, kind, server
        default_min = STATUS_DEFAULT_MINUTES[s_type]
        label = f"{default_min // 60}시간" if default_min % 60 == 0 else f"{default_min}분"

        btn_default = discord.ui.Button(label=label, style=discord.ButtonStyle.success)
        btn_default.callback = self.btn_default_callback
        self.add_item(btn_default)

        btn_manual = discord.ui.Button(label="직접 시간 작성", style=discord.ButtonStyle.secondary)
        btn_manual.callback = self.btn_manual_callback
        self.add_item(btn_manual)

    async def btn_default_callback(self, interaction: discord.Interaction):
        await finalize_status_report(self.bot, interaction, self.s_type, self.kind, self.server, STATUS_DEFAULT_MINUTES[self.s_type])

    async def btn_manual_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StatusManualTimeModal(self.bot, self.s_type, self.kind, self.server))


class StatusKindSelectView(discord.ui.View):
    """2단계(축복) 또는 1단계(에다니아): 축복/에다니아 종류를 선택. server는 축복일 때만 이미 정해져 넘어옵니다."""

    def __init__(self, bot: commands.Bot, s_type, server="통합"):
        super().__init__(timeout=120)
        self.bot, self.s_type, self.server = bot, s_type, server
        for kind in STATUS_KIND_OPTIONS[s_type]:
            btn = discord.ui.Button(label=kind, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(kind)
            self.add_item(btn)

    def _make_callback(self, kind):
        async def callback(interaction: discord.Interaction):
            s_title = STATUS_TITLES[self.s_type]
            desc = f"**종류:** {kind}\n"
            if self.server != "통합":
                desc += f"**서버:** {self.server}\n"
            desc += "\n남은 시간을 선택해주세요."
            embed = discord.Embed(title=f"{s_title} 제보", description=desc, color=0x2b2d31)
            await interaction.response.edit_message(embed=embed, view=StatusTimeSelectView(self.bot, self.s_type, kind, self.server))
        return callback


# 서버 번호는 로마자(Ⅰ/Ⅱ/Ⅲ)로 표기합니다.
_ROMAN_NUMBERS = {"1": "Ⅰ", "2": "Ⅱ", "3": "Ⅲ"}


class StatusServerNumberSelectView(discord.ui.View):
    """축복 전용: 지역을 고른 뒤 채널 번호(Ⅰ/Ⅱ/Ⅲ)를 선택 → 종류(해축/땅축/달축) 선택으로 이동.
    (서버 → 종류 → 시간, 3단계입니다.)"""

    def __init__(self, bot: commands.Bot, s_type, region):
        super().__init__(timeout=120)
        self.bot, self.s_type, self.region = bot, s_type, region
        numbers = STATUS_SERVER_NUMBERS.get(region, ["1", "2", "3"])
        for num in numbers:
            roman = _ROMAN_NUMBERS.get(num, num)
            btn = discord.ui.Button(label=f"{region}{roman}", style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(f"{region}{roman}")
            self.add_item(btn)

    def _make_callback(self, server):
        async def callback(interaction: discord.Interaction):
            s_title = STATUS_TITLES[self.s_type]
            embed = discord.Embed(title=f"{s_title} 제보", description=f"**서버:** {server}\n\n종류를 선택해주세요.", color=0x2b2d31)
            await interaction.response.edit_message(embed=embed, view=StatusKindSelectView(self.bot, self.s_type, server))
        return callback


class StatusServerSelectView(discord.ui.View):
    """1단계 첫 번째: 지역 선택 (아침의 축복 전용 진입점)."""

    def __init__(self, bot: commands.Bot, s_type):
        super().__init__(timeout=120)
        self.bot, self.s_type = bot, s_type
        for region in STATUS_SERVER_REGIONS:
            btn = discord.ui.Button(label=region, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(region)
            self.add_item(btn)

    def _make_callback(self, region):
        async def callback(interaction: discord.Interaction):
            s_title = STATUS_TITLES[self.s_type]
            embed = discord.Embed(title=f"{s_title} 제보", description=f"**지역:** {region}\n\n채널 번호를 선택해주세요.", color=0x2b2d31)
            await interaction.response.edit_message(embed=embed, view=StatusServerNumberSelectView(self.bot, self.s_type, region))
        return callback


async def finalize_status_report(bot: commands.Bot, interaction: discord.Interaction, s_type, kind, server, minutes):
    s_title = STATUS_TITLES[s_type]
    now = datetime.now(KST)
    expire_time = now + timedelta(minutes=minutes)
    # 남은 시간은 expire_time 컬럼으로 별도 관리되므로 content엔 중복 저장하지 않습니다.
    # 축복(blessing)은 kind가 없습니다 — "서버"만 저장합니다.
    report_content = f"{kind} | {server}" if kind else server

    conn = get_db()
    conn.execute(
        "INSERT INTO status_reports (type, user_id, content, timestamp, expire_time) VALUES (?, ?, ?, ?, ?)",
        (s_type, interaction.user.id, report_content, now.strftime('%Y-%m-%d %H:%M:%S'), expire_time.strftime('%Y-%m-%d %H:%M:%S')),
    )
    # status_alert_users는 값을 넣는 명령어가 없어 항상 비어있습니다(사용자 확인 후 미구현 유지).
    # 즉 아래 루프는 현재 아무에게도 DM을 보내지 않는 상태입니다.
    alert_users = conn.execute("SELECT user_id FROM status_alert_users").fetchall()
    conn.commit()

    if kind:
        confirm_detail = f"**{kind}** / **{server}** / {minutes}분"
    else:
        confirm_detail = f"**{server}** / {minutes}분"
    await interaction.response.send_message(f"✅ {s_title} 제보가 등록되었습니다! ({confirm_detail})", ephemeral=True)

    target_channel = await get_status_notify_channel(bot, s_type, interaction.channel)
    expire_ts = int(expire_time.timestamp())
    location_label = f"{kind} {server}" if kind else server
    report_text = f"[ {interaction.user.mention} ]\n@{location_label} <t:{expire_ts}:t>까지"
    try:
        public_msg = await target_channel.send(report_text)

        async def _delete_later(msg):
            await asyncio.sleep(180)
            try:
                await msg.delete()
            except Exception:
                pass
        bot.loop.create_task(_delete_later(public_msg))
    except Exception:
        pass

    dm_embed = discord.Embed(title=f"새로운 {s_title} 제보 도착!", color=0x3498db)
    if kind:
        dm_embed.description = f"**종류:** {kind}\n**서버:** {server}\n**남은 시간:** {minutes}분"
    else:
        dm_embed.description = f"**서버:** {server}\n**남은 시간:** {minutes}분"
    for (u_id,) in alert_users:
        user = bot.get_user(u_id)
        if user and user.id != interaction.user.id:
            try:
                await user.send(embed=dm_embed)
            except Exception:
                pass


# ==========================================
# [현황 조회 / 삭제]
# ==========================================
class StatusDeleteSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [
            discord.SelectOption(label=(content[:90] if content else "제보"), value=str(rid), description=ts)
            for rid, content, ts in rows[:25]
        ]
        super().__init__(placeholder="삭제할 제보를 선택하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        report_id = int(self.values[0])
        _delete_status_report(report_id)
        await interaction.response.send_message("제보를 삭제했습니다.", ephemeral=True)
        schedule_ephemeral_delete(interaction)


class StatusDeleteSelectView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=60)
        self.add_item(StatusDeleteSelect(rows))


class StatusViewPanel(discord.ui.View):
    def __init__(self, bot: commands.Bot, s_type, s_title):
        super().__init__(timeout=None)
        self.bot, self.st, self.stitle = bot, s_type, s_title
        # 원래 edana에만 있던 '내 제보 삭제' 버튼은 요청에 따라 제거했습니다
        # (blessing에도 추가하지 않음 — 완전히 없애는 것이 요청 사항이었습니다).

    @discord.ui.button(label="위치 제보하기", style=discord.ButtonStyle.primary, custom_id="위치 제보하기")
    async def br(self, i: discord.Interaction, b: discord.ui.Button):
        if self.st == "blessing":
            # 아침의 축복: 서버(발레노스Ⅰ/Ⅱ/Ⅲ) → 종류 → 시간 순서로 진행.
            # 지역이 발레노스 하나뿐이라 지역 선택 단계는 생략하고 바로 채널번호를 보여줍니다.
            only_region = STATUS_SERVER_REGIONS[0]
            embed = discord.Embed(title=f"{self.stitle} 제보", description="서버를 선택해주세요.", color=0x2b2d31)
            await i.response.send_message(embed=embed, view=StatusServerNumberSelectView(self.bot, self.st, only_region), ephemeral=True)
        else:
            # 에다니아: 서버 통합이라 종류 → 시간 순서 그대로 유지
            embed = discord.Embed(title=f"{self.stitle} 제보", description="종류를 선택해주세요.", color=0x2b2d31)
            await i.response.send_message(embed=embed, view=StatusKindSelectView(self.bot, self.st), ephemeral=True)

    @discord.ui.button(label="위치 현황 보기", style=discord.ButtonStyle.primary, custom_id="status_view_btn")
    async def bf(self, i: discord.Interaction, b: discord.ui.Button):
        rows = _get_status_rows(self.st)
        act = []
        for c, _, ex in rows:
            if not ex:
                continue
            rs = (datetime.strptime(ex, '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST) - datetime.now(KST)).total_seconds()
            if rs > 0:
                parts = c.split(" | ")
                if len(parts) == 2:
                    k, s = parts[0], parts[1]
                else:
                    k, s = None, parts[0]
                act.append((k, s, int(rs)))
        e = discord.Embed(title=f"{self.stitle} 현황", color=0x9b59b6)
        if not act:
            e.description = "현재 활성화된 제보가 없습니다."
        else:
            e.description = "\n\n".join([
                (f"**{k}** | {s}\n남은 시간: **{rs//60}분**" if k else f"**{s}**\n남은 시간: **{rs//60}분**")
                for k, s, rs in act
            ])
        await i.response.send_message(embed=e, ephemeral=True)
        schedule_ephemeral_delete(i)

    async def _delete_report(self, interaction: discord.Interaction):
        rows = _get_user_status_rows(self.st, interaction.user.id)
        if not rows:
            await interaction.response.send_message("삭제할 본인의 제보가 없습니다.", ephemeral=True)
            schedule_ephemeral_delete(interaction)
            return
        if len(rows) == 1:
            _delete_status_report(rows[0][0])
            await interaction.response.send_message("제보를 삭제했습니다.", ephemeral=True)
            schedule_ephemeral_delete(interaction)
            return
        embed = discord.Embed(title=f"{self.stitle} 제보 삭제", description="삭제할 제보를 선택해주세요.", color=0xe74c3c)
        await interaction.response.send_message(embed=embed, view=StatusDeleteSelectView(rows), ephemeral=True)
        schedule_ephemeral_delete(interaction)


# ==========================================
# [Cog / 슬래시 커맨드]
# ==========================================
class StatusReportCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="설치-축복제보", description="아침의 축복 제보 채널용 패널 설치")
    @app_commands.default_permissions(administrator=True)
    async def setup_bless(self, interaction: discord.Interaction):
        embed = discord.Embed(title="아침의 축복 현황", description="하단 버튼을 통해 새로운 위치를 제보하거나 현황을 확인하세요.", color=0x2b2d31)
        msg = await interaction.channel.send(embed=embed, view=StatusViewPanel(self.bot, "blessing", "아침의 축복"))
        save_panel("blessing", msg)
        await interaction.response.send_message("설치 완료", ephemeral=True)

    @app_commands.command(name="설치-에다니아제보", description="에다니아 제보 채널용 패널 설치")
    @app_commands.default_permissions(administrator=True)
    async def setup_edana(self, interaction: discord.Interaction):
        embed = discord.Embed(title="에다니아 현황", description="하단 버튼을 통해 새로운 위치를 제보하거나 현황을 확인하세요.", color=0x2b2d31)
        msg = await interaction.channel.send(embed=embed, view=StatusViewPanel(self.bot, "edana", "에다니아"))
        save_panel("edana", msg)
        await interaction.response.send_message("설치 완료", ephemeral=True)

    @app_commands.command(name="제보채널확인", description="[관리자] 현재 설정된 축복/에다니아 제보 채널을 확인합니다")
    @app_commands.default_permissions(administrator=True)
    async def check_status_notify_channel(self, interaction: discord.Interaction):
        conn = get_db()
        rows = dict(conn.execute("SELECT s_type, channel_id FROM status_notify_channels").fetchall())
        lines = []
        for s_type, label in [("blessing", "아침의 축복"), ("edana", "에다니아")]:
            if s_type in rows:
                ch = self.bot.get_channel(rows[s_type])
                lines.append(f"**{label}**: {ch.mention if ch else f'(알 수 없음, id={rows[s_type]})'}")
            else:
                lines.append(f"**{label}**: 지정 안 됨 (패널이 설치된 채널로 전송됩니다)")
        embed = discord.Embed(title="제보 알림 채널 설정 현황", description="\n".join(lines), color=0x2b2d31)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="제보채널지정", description="[관리자] 축복/에다니아 제보를 이 채널로 몰아서 보내도록 지정합니다")
    @app_commands.describe(종류="어떤 제보를 이 채널로 보낼지 선택하세요")
    @app_commands.choices(종류=[
        app_commands.Choice(name="아침의 축복", value="blessing"),
        app_commands.Choice(name="에다니아", value="edana"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def set_status_notify_channel(self, interaction: discord.Interaction, 종류: app_commands.Choice[str]):
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO status_notify_channels (s_type, channel_id) VALUES (?, ?)", (종류.value, interaction.channel.id))
        conn.commit()
        await interaction.response.send_message(f"✅ **{종류.name}** 제보가 이제 이 채널({interaction.channel.mention})로 전송됩니다.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusReportCog(bot))
