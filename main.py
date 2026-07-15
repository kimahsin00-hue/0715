"""
봇 진입점.

EXTENSIONS 리스트에 옮긴 cog들을 하나씩 추가해 나가고 있습니다 (현재: calculators,
sniper, bdo_time, item_lookup, dark_rift, boss_alert, status_report, tickets,
party_system, weekly_dm). 남은 것: coupon, chzzk_alert. 전부 옮기기 전까지는
기존 cian24.py를 실서버에서 그대로 운영하고, 이 main.py는 로컬 검증용으로만 씁니다.

persistent view 등록은 반드시 on_ready가 아니라 setup_hook에서, 딱 한 번만 하도록
합니다.
"""
import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

from db import init_db

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True


class BdoBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        init_db()

        # 지금까지 옮긴 cog들. 앞으로 하나씩 늘어납니다.
        EXTENSIONS = [
            "cogs.calculators",
            "cogs.sniper",
            "cogs.bdo_time",
            "cogs.item_lookup",
            "cogs.dark_rift",
            "cogs.boss_alert",
            "cogs.status_report",
            "cogs.tickets",
            "cogs.party_system",
            "cogs.weekly_dm",
            "cogs.watchdog",  # 다른 모든 cog의 View/embed를 참조하므로 맨 마지막에 로드
        ]
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        # 아이템 DB 로드 (없으면 로컬 백업 파일 사용, 있으면 그대로 사용)
        from market_api import load_item_dump
        print("📥 검은사막 아이템 DB 로드 중...")
        await load_item_dump(force=False)

        # persistent view는 여기서 딱 한 번만 등록합니다 (재연결마다 여러 번 불리는
        # on_ready에서 등록하면 안 됩니다 — 원본에서 패널이 사라지던 원인 중 하나).
        from cogs.bdo_time import BdoTimeView
        self.add_view(BdoTimeView())

        # DarkRiftActionView는 cog 상태(bot.loop.create_task 등)를 참조해야 해서
        # 인스턴스를 직접 받습니다. get_cog()는 load_extension 이후에만 유효합니다.
        from cogs.dark_rift import DarkRiftActionView
        dark_rift_cog = self.get_cog("DarkRiftCog")
        self.add_view(DarkRiftActionView(dark_rift_cog))

        from cogs.boss_alert import SetupBossView, BossDaySelectView
        self.add_view(SetupBossView())
        self.add_view(BossDaySelectView())

        from cogs.status_report import StatusViewPanel
        self.add_view(StatusViewPanel(self, "blessing", "아침의 축복"))
        self.add_view(StatusViewPanel(self, "edana", "에다니아"))

        from cogs.tickets import (
            SetupJoinView, QnaTicketView, ReportTicketView, AnonTicketView,
            AbsenceTicketView, CloseTicketView,
        )
        self.add_view(SetupJoinView())
        self.add_view(QnaTicketView())
        self.add_view(ReportTicketView())
        self.add_view(AnonTicketView())
        self.add_view(AbsenceTicketView(self))
        self.add_view(CloseTicketView(self))

        from cogs.party_system import PartyTicketView, PartyCalendarView, CloseGroupChannelView
        self.add_view(PartyTicketView())
        self.add_view(PartyCalendarView())
        self.add_view(CloseGroupChannelView())

        await self.tree.sync()


bot = BdoBot()


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game("시나모롤 길드 도우미 V2"))
    print(f'봇 로그인 성공: {bot.user.name}')


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
