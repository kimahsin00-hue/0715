"""
Pearl Abyss 계정 완전 자동 로그인 스크립트.
ID/PW/2차 비밀번호를 .env에서 읽어 자동 입력 후 storage_state.json 저장.
"""
import os
import sys
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

PA_ID = os.environ.get("PA_ID")
PA_PW = os.environ.get("PA_PW")
PA_SECOND_PW = os.environ.get("PA_SECOND_PW")
STORAGE_STATE_PATH = os.environ.get("PA_STORAGE_STATE_PATH", "storage_state.json")
TARGET_URL = "https://trade.kr.playblackdesert.com/Home/list/hot"


def main():
    if not (PA_ID and PA_PW and PA_SECOND_PW):
        print("PA_ID / PA_PW / PA_SECOND_PW 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(TARGET_URL, timeout=30000)
            page.wait_for_load_state("networkidle")

            page.wait_for_selector('input[placeholder="이메일"]', timeout=15000)
            page.fill('input[placeholder="이메일"]', PA_ID)
            page.fill('input[placeholder="비밀번호"]', PA_PW)
            page.click('button:has-text("로그인")')
            page.wait_for_load_state("networkidle")

            page.wait_for_selector("text=게임에서 사용중인 2차 비밀번호", timeout=15000)
            second_pw_input = page.locator("input").last
            second_pw_input.fill(PA_SECOND_PW)
            page.click('button:has-text("확인")')
            page.wait_for_load_state("networkidle")

            final_url = page.url

            if "account.pearlabyss.com" in final_url:
                print("로그인 실패 - 여전히 로그인 페이지에 있음")
                page.screenshot(path="auto_login_fail.png")
                browser.close()
                sys.exit(1)

            context.storage_state(path=STORAGE_STATE_PATH)
            print(f"자동 로그인 성공, 세션 저장 완료: {STORAGE_STATE_PATH}")
            browser.close()

        except Exception as e:
            print(f"자동 로그인 실패: {type(e).__name__}: {e}")
            try:
                page.screenshot(path="auto_login_fail.png")
            except Exception:
                pass
            browser.close()
            sys.exit(1)


if __name__ == "__main__":
    main()
