"""
거래소 시세 조회 (PA 공식 API + arsha.io 폴백) 및 아이템 DB.

ITEM_LIST는 여기서 관리하는 단일 소스입니다. 다른 모듈(예: 아이템DB 갱신 명령어를 담을
admin_util cog)에서 이 dict를 갱신하려면 재할당하지 말고 반드시 .update()를 쓰세요.
dict는 참조로 공유되므로 .update()만 하면 이 모듈을 import한 모든 곳에 즉시 반영됩니다.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime

from curl_cffi import requests as cffi_requests
import aiohttp

from config import KST, ITEM_DB_FILE, PA_STORAGE_STATE_PATH, PA_ALERT_CHANNEL_ID
from db import get_db
from data.game_data import FALLBACK_PRICES, SOV_RECIPES, SOV_WEAPON_NAME_PATTERNS
from data.item_data import HARDCODED_ITEMS

ITEM_LIST: dict[str, int] = {}

AUTO_LOGIN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_pa_login.py")


def load_local_backup() -> str:
    """
    네트워크 요청 없이 items_v2.json만 즉시 로드합니다 (동기 함수).
    봇 시작 시 이것만 호출해서 arsha dump 성공/실패와 무관하게 바로 뜨도록 합니다.
    실제 arsha dump 갱신은 refresh_item_dump_live()가 담당 (수동 명령어 / 매일 자동 루프).
    """
    if os.path.exists(ITEM_DB_FILE):
        with open(ITEM_DB_FILE, "r", encoding="utf-8") as f:
            backup = json.load(f)
        ITEM_LIST.update(backup)
        ITEM_LIST.update(HARDCODED_ITEMS)
        msg = f"로컬 백업({ITEM_DB_FILE}) 즉시 로드: {len(backup)}개. 현재 ITEM_LIST 총 {len(ITEM_LIST)}개"
        print(msg)
        return msg
    else:
        ITEM_LIST.update(HARDCODED_ITEMS)
        msg = f"로컬 백업 없음. HARDCODED {len(HARDCODED_ITEMS)}개만 우선 로드 (곧 /아이템디비갱신 필요)"
        print(msg)
        return msg


async def refresh_item_dump_live(force: bool = False) -> str:
    """
    arsha 라이브 dump를 새로 받아서 ITEM_LIST와 items_v2.json을 갱신합니다.
    - /아이템디비갱신 명령어가 수동 호출
    - item_lookup cog의 daily_item_db_refresh 루프가 매일 자동 호출
    dump 요청이 실패했을 때만 기존 로컬 백업으로 폴백합니다 (ITEM_LIST는 그대로 유지됨).
    반환값: 결과 요약 문자열 (로그/응답용)
    """
    try:
        url = "https://api.arsha.io/util/db/dump?lang=kr"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(ssl=False)) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    raise RuntimeError(f"dump API status {response.status}")
                data = await response.json()
                if not isinstance(data, list) or not data:
                    raise RuntimeError(f"dump 응답 비정상: type={type(data)}")

                temp = {}
                skipped = 0
                for item in data:
                    name = item.get("name")
                    id_raw = item.get("id")
                    if not name or id_raw is None:
                        skipped += 1
                        continue
                    iid = int(id_raw)
                    if name not in temp or iid < temp[name]:
                        temp[name] = iid
                ITEM_LIST.update(temp)
                ITEM_LIST.update(HARDCODED_ITEMS)

                with open(ITEM_DB_FILE, "w", encoding="utf-8") as f:
                    json.dump(ITEM_LIST, f, ensure_ascii=False, indent=2)

                msg = f"dump 갱신 완료: {len(data)}건 수신, {skipped}건 스킵\n현재 ITEM_LIST 총 {len(ITEM_LIST)}개"
                print(msg)
                return msg

    except Exception as e:
        print(f"dump 로드 실패: {type(e).__name__}: {e}")
        if os.path.exists(ITEM_DB_FILE):
            with open(ITEM_DB_FILE, "r", encoding="utf-8") as f:
                backup = json.load(f)
            ITEM_LIST.update(backup)
            ITEM_LIST.update(HARDCODED_ITEMS)
            msg = f"dump 실패, 로컬 백업 사용: {len(backup)}개 로드됨\n현재 ITEM_LIST 총 {len(ITEM_LIST)}개"
            print(msg)
            return msg
        else:
            msg = f"dump 실패 + 로컬 백업 없음. HARDCODED {len(HARDCODED_ITEMS)}개만 사용 중"
            print(msg)
            ITEM_LIST.update(HARDCODED_ITEMS)
            return msg


def save_to_cache(item_id, sid, price, stock, count):
    try:
        conn = get_db()
        c = conn.cursor()
        now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT OR REPLACE INTO item_cache VALUES (?, ?, ?, ?, ?, ?)",
                  (int(item_id), int(sid), int(price), int(stock), int(count), now_str))
        conn.commit()
    except Exception:
        pass


async def get_fallback_value(item_id, sid=0):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT price, stock, count FROM item_cache WHERE item_id=? AND sid=?", (int(item_id), int(sid)))
        row = c.fetchone()
        if row and row[0] is not None and int(row[0]) > 0:
            return int(row[0]), int(row[1]), int(row[2])
    except Exception:
        pass
    return FALLBACK_PRICES.get(int(item_id), (0, 0, 0))


async def fetch_arsha_sublist(item_id):
    url = "https://api.arsha.io/v2/kr/GetWorldMarketSubList"
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as session:
        async with session.get(url, params={"id": item_id, "lang": "kr"}, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"arsha API status {response.status}")
            data = await response.json(content_type=None)
            if isinstance(data, dict):
                for wrap_key in ("data", "items", "result", "list", "subList", "content"):
                    if isinstance(data.get(wrap_key), list):
                        data = data[wrap_key]
                        break
                else:
                    if any(k in data for k in ("basePrice", "price", "sid", "count")):
                        data = [data]
            if not isinstance(data, list):
                raise RuntimeError(f"응답 오류: {type(data)}")
            entries = []
            for row in data:
                try:
                    entries.append({
                        "item_id": int(row.get("mainKey") or row.get("id") or item_id),
                        "min_enhance": int(row.get("sid") or row.get("minEnhance") or 0),
                        "base_price": int(row.get("basePrice") or row.get("price") or 0),
                        "current_stock": int(row.get("count") or row.get("currentStock") or 0),
                        "total_trades": int(row.get("totalTrades") or 0),
                    })
                except Exception:
                    continue
            return entries


def _fetch_pa_sublist_sync(item_id):
    """
    curl_cffi 인증 세션(storage_state.json 쿠키 사용)으로 시세/재고 조회.
    동기 함수이므로 asyncio.to_thread로 감싸서 호출.
    세션 만료로 판단되면 RuntimeError("PA_SESSION_EXPIRED: ...")를 던짐.
    """
    session = _get_pa_session()
    resp = _pa_request_with_retry(
        session, "POST",
        "https://trade.kr.playblackdesert.com/Trademarket/GetWorldMarketSubList",
        json={"keyType": 0, "mainKey": item_id},
        timeout=12,
    )

    if "account.pearlabyss.com" in str(resp.url):
        raise RuntimeError("PA_SESSION_EXPIRED: 로그인 페이지로 리다이렉트됨 (세션 만료 추정)")

    raw = resp.content
    try:
        text = raw.decode("utf-16", errors="surrogatepass").encode("utf-8").decode("utf-8")
    except Exception:
        text = raw.decode("utf-8", errors="ignore")

    if "지원되지 않는 브라우저" in text:
        raise RuntimeError("PA_INCAPSULA_BLOCKED: Incapsula 차단 페이지 응답")

    data = json.loads(text)
    if data.get("resultCode") != 0:
        raise RuntimeError("PA_API_ERROR: PA API 에러")

    entries = []
    for chunk in data.get("resultMsg", "").split('|'):
        parts = chunk.split('-')
        if len(parts) >= 6:
            try:
                entries.append({
                    "item_id": int(parts[0]), "min_enhance": int(parts[1]),
                    "base_price": int(parts[3]), "current_stock": int(parts[4]), "total_trades": int(parts[5]),
                })
            except Exception:
                pass
    return entries


async def fetch_pa_sublist(item_id):
    try:
        entries = await asyncio.to_thread(_fetch_pa_sublist_sync, item_id)
        PA_SESSION_STATE["expired"] = False
        PA_SESSION_STATE["detail"] = ""
        return entries
    except Exception as e:
        msg = str(e)
        print(f"fetch_pa_sublist 실패: {msg}")
        if msg.startswith("PA_SESSION_EXPIRED") or msg.startswith("PA_TOKEN_NOT_FOUND"):
            PA_SESSION_STATE["expired"] = True
            PA_SESSION_STATE["detail"] = msg
        raise


async def fetch_market_sublist(item_id):
    """
    공식 웹 엔드포인트(PA, trade.kr.playblackdesert.com)를 메인으로 사용하고,
    실패했을 때만 arsha.io를 fallback으로 씁니다.
    """
    try:
        entries = await fetch_pa_sublist(item_id)
        if entries:
            return entries
        raise RuntimeError("PA 빈결과")
    except Exception:
        try:
            return await fetch_arsha_sublist(item_id)
        except Exception:
            return []


async def get_market_price(item_id, sid=0):
    try:
        item_id, sid = int(item_id), int(sid)
    except Exception:
        return 0, 0, 0
    try:
        entries = await fetch_market_sublist(item_id)
        if not entries:
            return await get_fallback_value(item_id, sid)
        for e in entries:
            if e["min_enhance"] == sid:
                p, s, t = e["base_price"], e["current_stock"], e["total_trades"]
                if p > 0:
                    save_to_cache(item_id, sid, p, s, t)
                    return p, s, t
        if sid == 0 and entries:
            p, s, t = entries[0]["base_price"], entries[0]["current_stock"], entries[0]["total_trades"]
            if p > 0:
                save_to_cache(item_id, sid, p, s, t)
                return p, s, t
        return await get_fallback_value(item_id, sid)
    except Exception:
        return await get_fallback_value(item_id, sid)


async def get_sov_weapon_price(item_key: str):
    req, exc = SOV_WEAPON_NAME_PATTERNS.get(item_key, ([], []))
    if not req:
        return (0, 0, 0)
    m_ids = [iid for n, iid in ITEM_LIST.items() if not any(e in n for e in exc) and all(r in n for r in req)]
    if not m_ids:
        return (0, 0, 0)
    results = await asyncio.gather(*[fetch_market_sublist(i) for i in m_ids], return_exceptions=True)
    bp, bs, bt = 0, 0, 0
    for entries in results:
        if isinstance(entries, Exception) or not entries:
            continue
        for e in entries:
            p = e.get("base_price", 0)
            if p > 0 and (bp == 0 or p < bp):
                bp, bs, bt = p, e.get("current_stock", 0), e.get("total_trades", 0)
    return (bp, bs, bt)


async def fetch_sov_prices(weapon_type: str):
    needed = set(name for _, ings in SOV_RECIPES[weapon_type] for name, _ in ings)
    needed.update(["마력의 파편", "카프라스의 돌"])

    async def _f(n):
        if n in ["황혼의 보석", "태초의 보석"]:
            return (0, 0, 0)
        if n in SOV_WEAPON_NAME_PATTERNS:
            return await get_sov_weapon_price(n)
        for d in [{"name": "마력의 파편", "id": 44195, "sid": 0}, {"name": "카프라스의 돌", "id": 721003, "sid": 0}]:
            if d["name"] == n:
                return await get_market_price(d["id"], d["sid"])
        return (0, 0, 0)

    needed_list = list(needed)
    results = await asyncio.gather(*[_f(n) for n in needed_list], return_exceptions=True)
    prices = {n: res if not isinstance(res, Exception) and res else (0, 0, 0) for n, res in zip(needed_list, results)}
    calc_p = prices.get("마력의 파편", (0,))[0] * 100 + prices.get("카프라스의 돌", (0,))[0] * 20000
    if "황혼의 보석" in needed:
        prices["황혼의 보석"] = (calc_p, 0, 0)
    if "태초의 보석" in needed:
        prices["태초의 보석"] = (calc_p, 0, 0)
    return prices


PA_SESSION_STATE = {
    "expired": False,
    "last_checked": None,
    "detail": "",
    "_alert_sent": False,
}

_pa_session_cache = {"session": None, "cookies_loaded_at": None}


def _load_pa_cookies() -> dict:
    """pa_login.py / auto_pa_login.py가 만든 storage_state.json에서 쿠키만 뽑아냅니다."""
    with open(PA_STORAGE_STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    cookies = {}
    for c in state.get("cookies", []):
        cookies[c["name"]] = c["value"]
    return cookies


def _get_pa_session() -> cffi_requests.Session:
    """
    curl_cffi 세션을 재사용합니다 (매 요청마다 새로 만들지 않음).
    storage_state.json이 갱신되면 캐시를 무효화하기 위해, 파일의 mtime을 확인해서
    바뀌었으면 세션을 다시 만듭니다.
    """
    mtime = os.path.getmtime(PA_STORAGE_STATE_PATH) if os.path.exists(PA_STORAGE_STATE_PATH) else None
    if _pa_session_cache["session"] is not None and _pa_session_cache["cookies_loaded_at"] == mtime:
        return _pa_session_cache["session"]

    cookies = _load_pa_cookies()
    session = cffi_requests.Session(impersonate="chrome124", http_version="v1")
    session.cookies.update(cookies)
    _pa_session_cache["session"] = session
    _pa_session_cache["cookies_loaded_at"] = mtime
    print(f"PA 세션 (재)로드: 쿠키 {len(cookies)}개")
    return session


def _pa_request_with_retry(session: cffi_requests.Session, method: str, url: str, retries: int = 3, **kwargs):
    last_err = None
    for attempt in range(retries):
        try:
            return session.request(method, url, **kwargs)
        except Exception as e:
            last_err = e
    raise last_err


def _fetch_pa_search_sync(keyword: str) -> list[dict]:
    """
    실제 동기 HTTP 작업. curl_cffi가 동기 라이브러리라 이 함수 자체는 블로킹입니다.
    반드시 asyncio.to_thread(...)로 감싸서 호출하세요 (아래 fetch_pa_search가 그렇게 함).
    세션이 만료된 것으로 판단되면 RuntimeError("PA_SESSION_EXPIRED: ...")를 던집니다.
    """
    session = _get_pa_session()

    resp = _pa_request_with_retry(
        session, "GET",
        "https://trade.kr.playblackdesert.com/Home/list/hot",
        timeout=10, allow_redirects=True,
    )

    if "account.pearlabyss.com" in str(resp.url):
        raise RuntimeError("PA_SESSION_EXPIRED: 로그인 페이지로 리다이렉트됨 (세션 만료 추정)")

    html = resp.text
    if "지원되지 않는 브라우저" in html:
        raise RuntimeError("PA_INCAPSULA_BLOCKED: Incapsula 차단 페이지 응답")

    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    if not m:
        print(f"토큰 못 찾음. 최종 URL: {resp.url}")
        print(f"HTML 앞부분:\n{html[:1000]}")
        raise RuntimeError("PA_TOKEN_NOT_FOUND: 토큰을 페이지에서 찾을 수 없음 (페이지 구조 변경 가능성)")
    token = m.group(1)

    resp = _pa_request_with_retry(
        session, "POST",
        "https://trade.kr.playblackdesert.com/Home/GetWorldMarketSearchList",
        data={"__RequestVerificationToken": token, "searchText": keyword},
        timeout=10,
    )
    data = json.loads(resp.text)

    if data.get("resultCode") != 0:
        raise RuntimeError(f"PA_SEARCH_ERROR: resultCode={data.get('resultCode')} msg={data.get('resultMsg')}")

    return [
        {
            "id": it["mainKey"],
            "name": it["name"],
            "grade": it.get("grade"),
            "sum_count": it.get("sumCount", 0),
            "total_sum_count": it.get("totalSumCount", 0),
        }
        for it in data.get("list", [])
    ]


async def fetch_pa_search(keyword: str) -> list[dict]:
    """
    PA 공식 거래소 이름 검색. /아이템디버그 등에서 이걸 호출하면 됩니다.
    세션이 만료된 것으로 감지되면 PA_SESSION_STATE["expired"]를 True로 세팅하고
    빈 리스트를 반환합니다 (예외를 상위로 던지지 않음).
    """
    try:
        results = await asyncio.to_thread(_fetch_pa_search_sync, keyword)
        PA_SESSION_STATE["expired"] = False
        PA_SESSION_STATE["detail"] = ""
        return results
    except Exception as e:
        msg = str(e)
        print(f"fetch_pa_search 실패: {msg}")
        if msg.startswith("PA_SESSION_EXPIRED") or msg.startswith("PA_TOKEN_NOT_FOUND"):
            PA_SESSION_STATE["expired"] = True
            PA_SESSION_STATE["detail"] = msg
        return []


async def try_auto_relogin() -> bool:
    """auto_pa_login.py를 서브프로세스로 실행해 재로그인. 성공하면 True."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, AUTO_LOGIN_SCRIPT],
            capture_output=True, text=True, timeout=90,
        )
        print(result.stdout)
        if result.returncode == 0:
            _pa_session_cache["session"] = None
            _pa_session_cache["cookies_loaded_at"] = None
            return True
        print(f"자동 재로그인 실패:\n{result.stderr}")
        return False
    except Exception as e:
        print(f"자동 재로그인 실행 중 예외: {e}")
        return False


async def check_and_alert_pa_session(bot) -> None:
    """
    주기적으로 호출하세요. 세션이 만료 상태면 먼저 자동 재로그인을 시도하고,
    성공하면 정상화 알림을, 실패하면 수동 조치가 필요하다는 알림을 관리자 채널에 보냅니다.
    알림 스팸을 막기 위해 실패 알림은 한 번만 보냅니다.
    """
    if not PA_SESSION_STATE["expired"]:
        return

    print("PA 세션 만료 감지 - 자동 재로그인 시도...")
    success = await try_auto_relogin()

    channel = bot.get_channel(PA_ALERT_CHANNEL_ID)

    if success:
        PA_SESSION_STATE["expired"] = False
        PA_SESSION_STATE["detail"] = ""
        PA_SESSION_STATE["_alert_sent"] = False
        if channel is not None:
            try:
                await channel.send("PA 세션이 만료되어 자동 재로그인으로 복구했습니다.")
            except Exception as e:
                print(f"PA 세션 복구 알림 전송 실패: {e}")
        return

    if PA_SESSION_STATE.get("_alert_sent"):
        return
    if channel is None:
        print("PA_ALERT_CHANNEL_ID 채널을 찾을 수 없어 알림을 보내지 못했습니다.")
        return
    try:
        await channel.send(
            "**PA 거래소 세션 만료 + 자동 재로그인도 실패했습니다.**\n"
            f"상세: `{PA_SESSION_STATE['detail']}`\n"
            "수동으로 `pa_login.py`를 실행해 세션을 갱신해주세요."
        )
        PA_SESSION_STATE["_alert_sent"] = True
    except Exception as e:
        print(f"PA 세션 만료 알림 전송 실패: {e}")


def reset_pa_session_alert():
    """storage_state.json을 갱신한 뒤 수동으로 호출하면 알림 스팸 방지 플래그를 초기화합니다."""
    PA_SESSION_STATE["expired"] = False
    PA_SESSION_STATE["detail"] = ""
    PA_SESSION_STATE["_alert_sent"] = False
