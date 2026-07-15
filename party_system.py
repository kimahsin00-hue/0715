"""
파티 시스템 cog — 가장 크고 tickets.py에 의존하는 cog입니다.

요청하신 대로 모달 필드를 '직업 / 공방합계 / 신청내용' 3개로 통일하고,
'신청내용' 칸의 placeholder만 피의제단(blood)과 나머지 던전이 다르게 나오도록 했습니다.
- 피의제단: "18층 이후 스펙 확인 후 신청 바람"
- 나머지: "그룹 채팅 디엠 시간 조율 완료 전까진 수시확인 부탁드립니다"

DB 컬럼명(timing)과 다운스트림 로직(파티 매칭, 패널 표시)은 그대로 유지했고,
임베드에 표시되는 라벨만 "희망 시간대" → "신청내용"으로 바꿨습니다.

get_ticket_category와 CloseTicketView는 tickets.py에서 그대로 가져와 씁니다
(원본의 SetupJoinView._get_ticket_category, CloseTicketView와 동일한 것).

원본 버그: 페세자/떼올룬/길드리그/푸른전장 4개 버튼 콜백 함수 이름이 전부
'peseza_btn'으로 중복되어 있었습니다. discord.py 데코레이터 특성상 동작 자체엔
문제없지만(각 버튼이 독립적으로 잘 등록됨), 헷갈리기 쉬워서 이름을 구분했습니다.

원본에 없던 save_panel() 호출을 /설치-파티신청, /설치-파티캘린더에도 추가했습니다
(bdo_time, 장기미접 때와 같은 watchdog 복구 누락 패턴).
"""
import asyncio
import re
import calendar as calendar_module
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from thefuzz import process

from config import (
    KST, ADMIN_ROLE_ID, WEEKDAYS, PARTY_NAMES, PARTY_EMOJI, PARTY_REQUIRED,
    TICKET_CATEGORY_ATO, TICKET_CATEGORY_SHRINE, TICKET_CATEGORY_BLOOD,
    TICKET_CATEGORY_PESEZA, TICKET_CATEGORY_TEOLLEUN, TICKET_CATEGORY_GUILDLEAGUE,
    TICKET_CATEGORY_BLUEWAR,
)
from db import get_db, save_panel
from utils import schedule_ephemeral_delete
from cogs.tickets import get_ticket_category, CloseTicketView

PARTY_CATEGORY_IDS = {
    "ato": TICKET_CATEGORY_ATO, "shrine": TICKET_CATEGORY_SHRINE, "blood": TICKET_CATEGORY_BLOOD,
    "peseza": TICKET_CATEGORY_PESEZA, "teolleun": TICKET_CATEGORY_TEOLLEUN,
    "guildleague": TICKET_CATEGORY_GUILDLEAGUE, "bluewar": TICKET_CATEGORY_BLUEWAR,
}

BLOOD_CONTENT_PLACEHOLDER = "18층 이후 스펙 확인 후 신청 바람"
DEFAULT_CONTENT_PLACEHOLDER = "그룹 채팅 디엠 시간 조율 완료 전까진 수시확인 부탁드립니다"


def _find_member_by_nickname(guild: discord.Guild, name: str):
    """참여인원 텍스트(닉네임)를 서버 멤버와 퍼지매칭해서 가장 비슷한 멤버를 찾는다."""
    name = name.strip()
    if not name:
        return None
    candidates = {m.display_name: m for m in guild.members if not m.bot}
    if not candidates:
        return None
    result = process.extractOne(name, candidates.keys())
    if result and result[1] >= 70:
        return candidates[result[0]]
    return None


def build_party_embed() -> discord.Embed:
    """현재 party_applications DB를 읽어 패널 임베드를 생성한다."""
    conn = get_db()
    embed = discord.Embed(title="⚔️ 파티 신청", color=0x9b59b6)
    total = 0
    for kind, name in PARTY_NAMES.items():
        rows = conn.execute(
            "SELECT user_name, job, stats, timing FROM party_applications WHERE kind=? ORDER BY created_at ASC",
            (kind,),
        ).fetchall()
        total += len(rows)
        if rows:
            lines = [f"{PARTY_EMOJI[kind]} **{name}** ({len(rows)}명)"]
            for i, (uname, job, stats, content) in enumerate(rows, 1):
                lines.append(f"`{i}.` {uname} │ {job} │ {stats} │ {content}")
            embed.add_field(name="\u200b", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name=f"{PARTY_EMOJI[kind]} {name}", value="신청자 없음", inline=True)
    if total > 0:
        embed.set_footer(text=f"총 {total}명 신청 중")
    embed.description = "아래 버튼으로 파티에 신청하세요."
    return embed


async def update_party_panel(guild: discord.Guild):
    """party_panel 테이블에 저장된 패널 메시지를 최신 신청 현황으로 업데이트한다."""
    conn = get_db()
    row = conn.execute("SELECT channel_id, message_id FROM party_panel WHERE guild_id=?", (guild.id,)).fetchone()
    if not row:
        return
    ch = guild.get_channel(row[0])
    if not ch:
        return
    try:
        msg = await ch.fetch_message(row[1])
        await msg.edit(embed=build_party_embed())
    except Exception as e:
        print(f"⚠️ 파티 패널 업데이트 실패: {e}")


async def update_party_calendar_panel(guild: discord.Guild):
    """party_calendar_panel 테이블에 저장된 캘린더 패널 메시지를 이번 달 기준으로 새로고침한다."""
    conn = get_db()
    row = conn.execute("SELECT channel_id, message_id FROM party_calendar_panel WHERE guild_id=?", (guild.id,)).fetchone()
    if not row:
        return
    ch = guild.get_channel(row[0])
    if not ch:
        return
    try:
        msg = await ch.fetch_message(row[1])
        now = datetime.now(KST)
        await msg.edit(embed=build_calendar_embed(guild.id, now.year, now.month))
    except Exception as e:
        print(f"⚠️ 파티 캘린더 패널 업데이트 실패: {e}")


async def create_party_group_channel(guild: discord.Guild, kind: str, user_ids: list, user_names: list):
    cat = await get_ticket_category(guild, PARTY_CATEGORY_IDS[kind])

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    mentions = []
    for uid in user_ids:
        member = guild.get_member(uid)
        if member:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            mentions.append(member.mention)
        else:
            mentions.append(f"<@{uid}>")

    ch_name = f"그룹-{PARTY_NAMES[kind]}-{datetime.now(KST).strftime('%m%d%H%M')}"
    topic = f"party_group:{kind}:{','.join(str(u) for u in user_ids)}"

    try:
        ch = await cat.create_text_channel(name=ch_name, overwrites=overwrites, topic=topic)
        embed = discord.Embed(title=f"🎉 {PARTY_NAMES[kind]} 파티 매칭 완료!", color=0x2ecc71)
        embed.description = (
            "파티 인원이 모두 모여 전용 채널이 생성되었습니다.\n이 채널은 **참여자와 관리자**만 볼 수 있습니다.\n"
            "일정 조율이 끝나면 **완료** 버튼으로 캘린더에 일정을 등록하고, 그 후 **파티 채널 닫기** 버튼으로 채널을 정리해주세요."
        )
        embed.add_field(name="참여자", value=", ".join(user_names), inline=False)
        await ch.send(content=" ".join(mentions), embed=embed, view=CloseGroupChannelView())
    except Exception as e:
        print(f"파티 그룹 채널 생성 실패: {e}")


class PartyModal(discord.ui.Modal):
    def __init__(self, kind: str):
        super().__init__(title=f'{PARTY_NAMES[kind]} 파티 신청')
        self.kind = kind
        self.job = discord.ui.TextInput(label='직업', placeholder='예: 워리어, 레인저 등', required=True, max_length=20)
        self.stats = discord.ui.TextInput(label='공/방 합계', placeholder='예: 공300 방330', required=True, max_length=30)

        content_placeholder = BLOOD_CONTENT_PLACEHOLDER if kind == "blood" else DEFAULT_CONTENT_PLACEHOLDER
        self.content = discord.ui.TextInput(
            label='신청내용',
            style=discord.TextStyle.paragraph,
            required=True,
            placeholder=content_placeholder,
            max_length=500,
        )
        self.add_item(self.job)
        self.add_item(self.stats)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        schedule_ephemeral_delete(interaction)
        cat = await get_ticket_category(interaction.guild, PARTY_CATEGORY_IDS[self.kind])
        ch_name = f"{self.kind}-{interaction.user.name}-{datetime.now(KST).strftime('%m%d%H%M')}"

        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO party_applications (kind, user_id, user_name, job, stats, timing) VALUES (?,?,?,?,?,?)",
            (self.kind, interaction.user.id, interaction.user.display_name, self.job.value, self.stats.value, self.content.value),
        )
        app_id = c.lastrowid
        conn.commit()

        c.execute("SELECT id, user_id, user_name FROM party_applications WHERE kind=? ORDER BY created_at ASC", (self.kind,))
        apps = c.fetchall()
        required = PARTY_REQUIRED[self.kind]

        if len(apps) >= required:
            target_apps = apps[:required]
            target_ids = [t[0] for t in target_apps]
            user_ids = [t[1] for t in target_apps]
            user_names = [t[2] for t in target_apps]

            c.executemany("DELETE FROM party_applications WHERE id=?", [(tid,) for tid in target_ids])
            conn.commit()

            ch = await cat.create_text_channel(name=ch_name, topic=f"app_id:0|user_id:{interaction.user.id}")
            embed = discord.Embed(title=f"{PARTY_NAMES[self.kind]} 파티 신청 완료", color=0x9b59b6)
            embed.add_field(name="신청자", value=interaction.user.mention, inline=False)
            embed.add_field(name="상태", value="파티가 즉시 매칭되었습니다!", inline=False)
            await ch.send(content=interaction.user.mention, embed=embed, view=CloseTicketView(interaction.client))

            await create_party_group_channel(interaction.guild, self.kind, user_ids, user_names)
            await interaction.followup.send(
                f"✅ 티켓이 생성되었습니다: {ch.mention}\n🎉 **파티 인원이 모두 충족되어 그룹 전용 채널이 생성되었습니다!**", ephemeral=True
            )

            # 마지막 신청자의 이 알림용 티켓(app_id:0, 실제 신청 레코드와 연결 안 됨)은
            # 다른 신청자들의 신청 폼처럼 자동으로 정리되지 않던 버그가 있었습니다.
            # 그룹 전용 채널이 이미 생성됐으므로 안내 확인 시간을 준 뒤 자동으로 닫아줍니다.
            async def _auto_close_match_ticket(channel):
                await asyncio.sleep(10)
                try:
                    await channel.delete()
                except Exception:
                    pass
            interaction.client.loop.create_task(_auto_close_match_ticket(ch))
        else:
            ch = await cat.create_text_channel(name=ch_name, topic=f"app_id:{app_id}|user_id:{interaction.user.id}")
            conn.execute("UPDATE party_applications SET channel_id=? WHERE id=?", (ch.id, app_id))
            conn.commit()

            embed = discord.Embed(title=f"{PARTY_NAMES[self.kind]} 파티 신청", color=0x9b59b6)
            embed.add_field(name="신청자", value=interaction.user.mention, inline=False)
            embed.add_field(name="직업", value=self.job.value, inline=True)
            embed.add_field(name="공/방 합계", value=self.stats.value, inline=True)
            embed.add_field(name="신청내용", value=self.content.value, inline=False)
            await ch.send(content=interaction.user.mention, embed=embed, view=CloseTicketView(interaction.client))
            await interaction.followup.send(f"✅ 티켓 생성됨: {ch.mention}", ephemeral=True)

        await update_party_panel(interaction.guild)


class PartyTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="아토락시온 신청", style=discord.ButtonStyle.primary, custom_id="party_ato", row=0)
    async def ato_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("ato"))

    @discord.ui.button(label="검은사당 신청", style=discord.ButtonStyle.primary, custom_id="party_shrine", row=0)
    async def shrine_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("shrine"))

    @discord.ui.button(label="피의제단 신청", style=discord.ButtonStyle.primary, custom_id="party_blood", row=0)
    async def blood_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("blood"))

    @discord.ui.button(label="페세자 신청", style=discord.ButtonStyle.primary, custom_id="party_peseza", row=1)
    async def peseza_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("peseza"))

    @discord.ui.button(label="떼올룬 신청", style=discord.ButtonStyle.primary, custom_id="party_teolleun", row=1)
    async def teolleun_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("teolleun"))

    @discord.ui.button(label="길드리그 신청", style=discord.ButtonStyle.primary, custom_id="party_guildleague", row=2)
    async def guildleague_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("guildleague"))

    @discord.ui.button(label="푸른전장 신청", style=discord.ButtonStyle.primary, custom_id="party_bluewar", row=2)
    async def bluewar_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PartyModal("bluewar"))


class PartyCompleteModal(discord.ui.Modal, title='파티 완료 등록'):
    def __init__(self, default_kind_label: str, default_names: str):
        super().__init__()
        self.item_input = discord.ui.TextInput(label='항목 (제단/아토/사당/페세자)', default=default_kind_label, required=True, max_length=20)
        self.names_input = discord.ui.TextInput(label='참여인원 닉네임', style=discord.TextStyle.paragraph, default=default_names, required=True, max_length=300)
        self.date_input = discord.ui.TextInput(label='날짜 (MM/DD)', placeholder='예: 07/15', required=True, max_length=5)
        self.time_input = discord.ui.TextInput(label='시간 (HH:MM)', placeholder='예: 21:00', required=True, max_length=5)
        self.add_item(self.item_input)
        self.add_item(self.names_input)
        self.add_item(self.date_input)
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mm_str, dd_str = self.date_input.value.strip().split("/")
            mm, dd = int(mm_str), int(dd_str)
            now = datetime.now(KST)
            year = now.year
            if mm < now.month - 6:
                year += 1
            datetime(year, mm, dd)
            event_date = f"{year:04d}-{mm:02d}-{dd:02d}"
        except Exception:
            await interaction.response.send_message("날짜는 MM/DD 형식으로 올바르게 입력해주세요. 예: 07/15", ephemeral=True)
            schedule_ephemeral_delete(interaction)
            return

        time_val = self.time_input.value.strip()
        conn = get_db()
        conn.execute(
            "INSERT INTO party_schedule (guild_id, kind, participants, event_date, event_time, created_by) VALUES (?,?,?,?,?,?)",
            (interaction.guild.id, self.item_input.value.strip(), self.names_input.value.strip(), event_date, time_val, interaction.user.id),
        )
        conn.commit()
        await interaction.response.send_message(
            f"✅ 캘린더에 등록되었습니다!\n**{self.item_input.value.strip()}** · {event_date} {time_val}\n참여: {self.names_input.value.strip()}"
        )
        await update_party_calendar_panel(interaction.guild)


class CloseGroupChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def _parse_topic(topic: str):
        try:
            _, kind, ids_part = (topic or "").split(":", 2)
            ids = [int(x) for x in ids_part.split(",") if x]
            return kind, ids
        except Exception:
            return "", []

    @discord.ui.button(label="파티 채널 닫기", style=discord.ButtonStyle.danger, custom_id="close_group_channel_btn")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("5초 후 파티 그룹 채널이 삭제됩니다.")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

    @discord.ui.button(label="완료", style=discord.ButtonStyle.success, custom_id="party_group_complete_btn")
    async def complete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        kind, member_ids = self._parse_topic(interaction.channel.topic)
        is_admin = getattr(interaction.user.guild_permissions, 'administrator', False) or interaction.user.get_role(ADMIN_ROLE_ID)
        if not (is_admin or interaction.user.id in member_ids):
            await interaction.response.send_message("이 파티에 참여한 인원만 사용할 수 있습니다.", ephemeral=True)
            schedule_ephemeral_delete(interaction)
            return
        default_names = []
        for uid in member_ids:
            m = interaction.guild.get_member(uid)
            default_names.append(m.display_name if m else str(uid))
        default_kind_label = PARTY_NAMES.get(kind, kind)
        await interaction.response.send_modal(PartyCompleteModal(default_kind_label, ", ".join(default_names)))


def _split_into_field_chunks(lines, limit=1024):
    chunks = []
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ["이번 달 등록된 일정이 없습니다."]


def build_calendar_embed(guild_id: int, year: int, month: int) -> discord.Embed:
    conn = get_db()
    rows = conn.execute(
        "SELECT kind, participants, event_date, event_time FROM party_schedule WHERE guild_id=? AND event_date LIKE ? ORDER BY event_date ASC, event_time ASC",
        (guild_id, f"{year:04d}-{month:02d}-%"),
    ).fetchall()

    events_by_day = {}
    for kind, participants, event_date, event_time in rows:
        day = int(event_date.split("-")[2])
        events_by_day.setdefault(day, []).append((kind, participants, event_time))

    cal = calendar_module.Calendar(firstweekday=6)
    weeks = cal.monthdayscalendar(year, month)

    grid_lines = ["일   월   화   수   목   금   토"]
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("    ")
            else:
                mark = "●" if day in events_by_day else " "
                cells.append(f"{day:>2}{mark} ")
        grid_lines.append("".join(cells))
    grid_text = "```\n" + "\n".join(grid_lines) + "\n```"

    embed = discord.Embed(title=f"📅 {year}년 {month}월 파티 일정 캘린더", color=0x3498db)
    embed.description = grid_text

    if events_by_day:
        agenda_lines = []
        for day in sorted(events_by_day):
            wd = WEEKDAYS[datetime(year, month, day).weekday()][0]
            for kind, participants, event_time in events_by_day[day]:
                agenda_lines.append(f"**{month}/{day}({wd})** {event_time} · {kind} · {participants}")
        chunks = _split_into_field_chunks(agenda_lines)
        for idx, chunk in enumerate(chunks):
            field_name = "📋 일정 상세" if idx == 0 else "\u200b"
            embed.add_field(name=field_name, value=chunk, inline=False)
    else:
        embed.add_field(name="📋 일정 상세", value="이번 달 등록된 일정이 없습니다.", inline=False)

    embed.set_footer(text=f"{year}-{month:02d} · ● 표시된 날짜에 일정이 있습니다 · 파티 완료 등록 시 자동 갱신")
    return embed


class CalendarDeleteSelect(discord.ui.Select):
    def __init__(self, rows):
        options = []
        for r_id, kind, parts, edate, etime in rows[:25]:
            short_date = edate[5:]
            label = f"[{short_date} {etime}] {kind}"
            desc = parts[:50] + ("..." if len(parts) > 50 else "")
            options.append(discord.SelectOption(label=label, description=desc, value=str(r_id)))
        super().__init__(placeholder="삭제할 일정을 선택하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        conn = get_db()
        conn.execute("DELETE FROM party_schedule WHERE id=?", (target_id,))
        conn.commit()
        await interaction.response.send_message("✅ 해당 일정이 성공적으로 삭제되었습니다.", ephemeral=True)
        await update_party_calendar_panel(interaction.guild)


class CalendarDeleteView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=60)
        self.add_item(CalendarDeleteSelect(rows))


class PartyCalendarView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def _parse_year_month(interaction: discord.Interaction):
        m = re.search(r'(\d+)년 (\d+)월', interaction.message.embeds[0].title if interaction.message.embeds else "")
        return (int(m.group(1)), int(m.group(2))) if m else (datetime.now(KST).year, datetime.now(KST).month)

    @discord.ui.button(label="◀ 이전달", style=discord.ButtonStyle.secondary, custom_id="party_cal_prev")
    async def p_m(self, i: discord.Interaction, b: discord.ui.Button):
        y, m = self._parse_year_month(i)
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        await i.response.edit_message(embed=build_calendar_embed(i.guild.id, y, m))

    @discord.ui.button(label="오늘", style=discord.ButtonStyle.primary, custom_id="party_cal_today")
    async def t(self, i: discord.Interaction, b: discord.ui.Button):
        now = datetime.now(KST)
        await i.response.edit_message(embed=build_calendar_embed(i.guild.id, now.year, now.month))

    @discord.ui.button(label="다음달 ▶", style=discord.ButtonStyle.secondary, custom_id="party_cal_next")
    async def n_m(self, i: discord.Interaction, b: discord.ui.Button):
        y, m = self._parse_year_month(i)
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        await i.response.edit_message(embed=build_calendar_embed(i.guild.id, y, m))

    @discord.ui.button(label="일정 삭제", style=discord.ButtonStyle.danger, custom_id="party_cal_delete")
    async def del_btn(self, i: discord.Interaction, b: discord.ui.Button):
        is_admin = getattr(i.user.guild_permissions, 'administrator', False) or i.user.get_role(ADMIN_ROLE_ID)
        if not is_admin:
            await i.response.send_message("관리자만 일정을 삭제할 수 있습니다.", ephemeral=True)
            return
        y, m = self._parse_year_month(i)
        conn = get_db()
        rows = conn.execute(
            "SELECT id, kind, participants, event_date, event_time FROM party_schedule WHERE guild_id=? AND event_date LIKE ? ORDER BY event_date ASC, event_time ASC",
            (i.guild.id, f"{y:04d}-{m:02d}-%"),
        ).fetchall()
        if not rows:
            await i.response.send_message("이 달에는 삭제할 일정이 없습니다.", ephemeral=True)
            return
        await i.response.send_message("삭제할 일정을 선택하세요.", view=CalendarDeleteView(rows), ephemeral=True)


class PartySystemCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.party_schedule_reminder_loop.start()
        # tickets.py의 CloseTicketModal이 파티 신청 삭제를 위해 참조하는 훅
        bot.update_party_panel = update_party_panel

    def cog_unload(self):
        self.party_schedule_reminder_loop.cancel()

    @tasks.loop(minutes=1)
    async def party_schedule_reminder_loop(self):
        """캘린더에 등록된 일정 1시간 전에 참여인원에게 DM을 보낸다."""
        now = datetime.now(KST)
        conn = get_db()
        today_str = now.strftime('%Y-%m-%d')
        rows = conn.execute(
            "SELECT id, guild_id, kind, participants, event_date, event_time FROM party_schedule WHERE reminded=0 AND event_date >= ?",
            (today_str,),
        ).fetchall()

        for sid, g_id, kind, participants, event_date, event_time in rows:
            try:
                event_dt = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            except Exception:
                conn.execute("UPDATE party_schedule SET reminded=1 WHERE id=?", (sid,))
                conn.commit()
                continue

            minutes_left = (event_dt - now).total_seconds() / 60
            if minutes_left > 60:
                continue

            conn.execute("UPDATE party_schedule SET reminded=1 WHERE id=?", (sid,))
            conn.commit()
            if minutes_left < 0:
                continue

            guild = self.bot.get_guild(g_id)
            if not guild:
                continue
            await guild.chunk()

            names = [n for n in re.split(r'[,\n]+', participants) if n.strip()]
            embed = discord.Embed(title="⏰ 파티 일정 1시간 전 알림", color=0xe67e22)
            embed.add_field(name="항목", value=kind, inline=True)
            embed.add_field(name="시간", value=f"{event_date} {event_time}", inline=True)
            embed.add_field(name="참여인원", value=participants, inline=False)

            for name in names:
                member = _find_member_by_nickname(guild, name)
                if not member:
                    print(f"⚠️ 일정 알림: '{name}' 님을 서버 멤버에서 찾지 못해 DM을 건너뜁니다. (schedule id={sid})")
                    continue
                try:
                    await member.send(embed=embed)
                    await asyncio.sleep(0.1)
                except Exception as e:
                    print(f"⚠️ 일정 알림 DM 실패 (member={member.id}): {e}")

    @party_schedule_reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="설치-파티신청", description="[관리자] 파티 신청 티켓 패널 설치")
    @app_commands.default_permissions(administrator=True)
    async def cmd_install_party(self, interaction: discord.Interaction):
        panel_msg = await interaction.channel.send(embed=build_party_embed(), view=PartyTicketView())
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO party_panel (guild_id, channel_id, message_id) VALUES (?,?,?)",
            (interaction.guild.id, interaction.channel.id, panel_msg.id),
        )
        conn.commit()
        save_panel("party", panel_msg)
        await interaction.response.send_message("✅ 파티신청 패널 설치 완료", ephemeral=True)

    @app_commands.command(name="설치-파티캘린더", description="[관리자] 파티 완료 일정 캘린더 패널 설치")
    @app_commands.default_permissions(administrator=True)
    async def cmd_install_party_calendar(self, interaction: discord.Interaction):
        now = datetime.now(KST)
        embed = build_calendar_embed(interaction.guild.id, now.year, now.month)
        panel_msg = await interaction.channel.send(embed=embed, view=PartyCalendarView())
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO party_calendar_panel (guild_id, channel_id, message_id) VALUES (?,?,?)",
            (interaction.guild.id, interaction.channel.id, panel_msg.id),
        )
        conn.commit()
        save_panel("party_calendar", panel_msg)
        await interaction.response.send_message("✅ 파티캘린더 패널 설치 완료", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PartySystemCog(bot))
