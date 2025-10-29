# send_horoscope_gchat.py
# -*- coding: utf-8 -*-
import os, re, time, json
from datetime import datetime
import requests
from bs4 import BeautifulSoup

ASKJIYUN_TODAY_URL = "https://askjiyun.com/today"
TITLE_PREFIX = "오늘의 운세,"           # 목록 제목 접두사
GCHAT_WEBHOOK = os.environ.get("GCHAT_WEBHOOK")  # 1)에서 복사한 URL을 환경변수로 넣어 사용 권장

UA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko,en;q=0.8",
    "Referer": "https://askjiyun.com/",
}

def kst_today_md():
    # 한국시간 기준 오늘 날짜 "M월 D일"
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except Exception:
        now = datetime.now()
    return now, f"{now.month}월 {now.day}일"

def http_get(url, retry=3, timeout=15):
    last = None
    for _ in range(retry):
        try:
            r = requests.get(url, headers=UA_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"GET 실패: {url} / {last}")

def to_abs(href):
    if not href: return None
    return href if href.startswith("http") else f"https://askjiyun.com/{href.lstrip('/')}"

def find_today_post_url():
    html = http_get(ASKJIYUN_TODAY_URL)
    soup = BeautifulSoup(html, "html.parser")

    # "오늘의 운세, o월 x일" 패턴의 a 태그 수집
    anchors = soup.find_all("a", string=re.compile(r"^오늘의 운세,\s*\d+월\s*\d+일"))
    if not anchors:
        # 텍스트가 분리되어 있거나 공백/개행이 섞인 경우 보정
        for a in soup.find_all("a"):
            txt = (a.get_text(" ", strip=True) or "").replace("\u00a0", " ")
            if re.match(r"^오늘의 운세,\s*\d+월\s*\d+일", txt):
                anchors.append(a)

    if not anchors:
        raise RuntimeError("목록에서 '오늘의 운세, o월 x일' 링크를 못 찾음")

    # 오늘 날짜 우선
    _, md = kst_today_md()
    for a in anchors:
        if md in a.get_text(" ", strip=True):
            return to_abs(a.get("href"))

    # 오늘 글이 목록에 없으면 가장 최근 글(첫 번째)
    return to_abs(anchors[0].get("href"))

def parse_post_text(post_html):
    soup = BeautifulSoup(post_html, "html.parser")
    # 본문 후보(가장 텍스트가 많은 것을 선택)
    candidates = soup.select(".xe_content, .read_body, article, .board_read .rd_body, .read") or [soup]
    body = max(candidates, key=lambda el: len(el.get_text(" ", strip=True)))
    text = body.get_text("\n", strip=True)
    # 깔끔하게 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()

def build_message(title, text, url):
    # Google Chat은 텍스트 본문에 간단 마크다운(굵게 등) 지원
    # 참고: 공식 'Format messages' 문서. 
    head = f"🌞 *{title}*\n(원문: {url})\n\n"
    # 너무 길면 잘라서 보냄
    if len(text) > 6000:
        text = text[:6000] + "\n...\n(전체 내용은 원문 링크에서 확인)"
    return head + text

def send_to_gchat(message):
    if not GCHAT_WEBHOOK:
        raise RuntimeError("환경변수 GCHAT_WEBHOOK 이 설정되어 있지 않습니다.")
    payload = {"text": message}
    r = requests.post(GCHAT_WEBHOOK, json=payload, timeout=15)
    r.raise_for_status()
    print("Google Chat 전송 완료")

def main():
    now, md = kst_today_md()
    expected_title = f"{TITLE_PREFIX} {md}"

    post_url = find_today_post_url()
    post_html = http_get(post_url)
    # 페이지 <title>에 '오늘의 운세'가 있으면 실제 제목으로 보정
    try:
        t = BeautifulSoup(post_html, "html.parser").find("title")
        if t and "오늘의 운세" in t.get_text():
            expected_title = t.get_text().split(" - ")[0].strip()
    except Exception:
        pass

    text = parse_post_text(post_html)
    message = build_message(expected_title, text, post_url)
    send_to_gchat(message)

if __name__ == "__main__":
    main()
