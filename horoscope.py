"""
매일경제(MK)에서 '오늘의 운세' 게시글을 찾아
Google Chat Incoming Webhook으로 전송하는 스크립트 (개인용).

특이사항:
- 주말 운세는 토/일 2일치가 한 게시글로 올라올 수 있음 (제목에 날짜가 2개).
- 게시글 본문은 텍스트가 아니라 이미지 2장으로 구성되는 경우가 있음.

설치: pip install requests beautifulsoup4
환경변수: GCHAT_WEBHOOK  (Google Chat에서 발급받은 웹훅 URL)
"""
import os
import re
import time
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
from typing import Optional, List

import requests
from bs4 import BeautifulSoup
import urllib.robotparser as robotparser

# ---------- 설정 ----------
MK_BASE = "https://www.mk.co.kr"
SEARCH_URL = "https://www.mk.co.kr/search?word=%EC%98%A4%EB%8A%98%EC%9D%98%20%EC%9A%B4%EC%84%B8"
# askjiyun (지윤철학원) 오늘의 운세 목록
BASE = "https://askjiyun.com"
ASKJIYUN_TODAY_LIST_URL = urljoin(BASE, "/today")
# 제목 예시:
# - 오늘의 운세 2025년 12월 15일 月(음력 10월 26일)
# - 오늘의 운세 2025년 12월 13일 土(음력 10월 24일)·2025년 12월 14일 日(음력 10월 25일)
TITLE_PREFIX = "오늘의 운세"
GCHAT_WEBHOOK = os.getenv("GCHAT_WEBHOOK")
GCHAT_THREAD_KEY = os.getenv("GCHAT_THREAD_KEY")
GCHAT_MESSAGE_REPLY_OPTION = os.getenv("GCHAT_MESSAGE_REPLY_OPTION", "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")

# 전송 최대 길이: 너무 길면 웹훅/채널에서 문제될 수 있으므로 안전하게 자름
MAX_MESSAGE_LEN = 14000
MAX_LIST_PAGES = 6  # 최대 몇 페이지까지 목록을 탐색할지 (1-based)

# MK는 신문 특성상 주말(토/일) 운세가 합본으로 올라오는 경우가 있습니다.
# 이 스크립트는 "토요일에도, 일요일에도" 같은 MK 주말 이미지를 함께 보내는 것을 기본으로 합니다.
# (토요일: 지윤 토요일 + MK 주말 이미지 / 일요일: 지윤 일요일 + MK 주말 이미지)
#
# 만약 같은 MK 글을 이틀 연속 보내기 싫으면 환경변수로 중복 전송을 끌 수 있습니다.
SKIP_MK_WEEKEND_DUPLICATE = os.getenv("SKIP_MK_WEEKEND_DUPLICATE", "").strip().lower() in ("1", "true", "yes", "y")

# GitHub Actions 등에서 간헐적으로 연결 지연이 있어 사이트별 timeout/retry를 분리
MK_TIMEOUT = float(os.getenv("MK_TIMEOUT", "15"))
MK_RETRY = int(os.getenv("MK_RETRY", "3"))
# requests timeout은 (connect, read) 튜플도 가능
ASKJIYUN_CONNECT_TIMEOUT = float(os.getenv("ASKJIYUN_CONNECT_TIMEOUT", "30"))
ASKJIYUN_READ_TIMEOUT = float(os.getenv("ASKJIYUN_READ_TIMEOUT", "30"))
ASKJIYUN_RETRY = int(os.getenv("ASKJIYUN_RETRY", "5"))

# HTTP 헤더 (간단한 브라우저처럼)
MK_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept-Language": "ko,en;q=0.8",
    "Referer": MK_BASE,
}

ASKJIYUN_HEADERS = {
    # ModSecurity(406) 회피를 위해 브라우저 헤더를 조금 더 흉내낸다.
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": BASE,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------- 유틸: robots 체크 ----------
def allowed_by_robots(url, base_url, user_agent="*"):
    try:
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        logging.warning("robots.txt 체크 실패: %s (계속 진행)", e)
        # robots 체크 실패 시에도 개인용으로 계속 진행하겠다면 True 반환
        return True


# ---------- HTTP 요청 with retry ----------
def http_get(url, *, headers=None, timeout=15, retry=3, backoff=1.2, backoff_factor=1.6):
    last_exc = None
    for i in range(1, retry + 1):
        try:
            resp = requests.get(url, headers=headers or MK_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exc = e
            logging.warning("GET 실패 (%s) %s (시도 %d/%d)", url, e, i, retry)
            time.sleep(backoff)
            backoff *= backoff_factor
    raise RuntimeError(f"GET 실패: {url} / {last_exc}")


# ---------- 목록에서 오늘 게시글 링크 찾기 ----------
def _mk_search_page_url(page: int) -> str:
    if page <= 1:
        return SEARCH_URL
    sep = "&" if "?" in SEARCH_URL else "?"
    return f"{SEARCH_URL}{sep}page={page}"


def _clean_title_text(text: str) -> str:
    return (text or "").replace("\u00a0", " ").strip()


def _extract_dates_from_title(title: str) -> list[date]:
    """제목에서 'YYYY년 M월 D일' 패턴을 모두 추출합니다."""
    if not title:
        return []
    out: list[date] = []
    for y, m, d in re.findall(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", title):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    # 순서 유지 + 중복 제거
    seen = set()
    uniq = []
    for dt in out:
        if dt in seen:
            continue
        seen.add(dt)
        uniq.append(dt)
    return uniq


def find_today_post_url():
    """검색 결과(여러 페이지)를 순회하며 오늘 날짜가 포함된 '오늘의 운세' 게시글 URL을 찾습니다.

    주말 운세처럼 날짜가 2개인 제목도 '오늘 날짜' 문자열이 포함되면 매칭됩니다.
    """
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today_token = f"{now.year}년 {now.month}월 {now.day}일"
    want = re.compile(rf"{re.escape(TITLE_PREFIX)}\s+.*{re.escape(today_token)}")

    fallback_links: list[str] = []
    for page in range(1, MAX_LIST_PAGES + 1):
        url = _mk_search_page_url(page)
        logging.debug("fetching search page %d: %s", page, url)
        try:
            html = http_get(url, headers=MK_HEADERS, timeout=MK_TIMEOUT, retry=MK_RETRY)
        except Exception as e:
            logging.warning("검색 페이지 가져오기 실패 (page %d): %s", page, e)
            continue

        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.find_all("a")
        for a in anchors:
            title = _clean_title_text(a.get_text(" ", strip=True))
            if TITLE_PREFIX not in title:
                continue
            href = a.get("href")
            if not href:
                continue
            post_url = urljoin(MK_BASE, href)
            fallback_links.append(post_url)
            if want.search(title):
                return post_url

    if fallback_links:
        return fallback_links[0]
    raise RuntimeError("검색 결과에서 '오늘의 운세' 게시글 링크를 찾지 못했습니다.")


def find_askjiyun_today_post_url():
    """askjiyun.com /today 목록에서 오늘 날짜의 '오늘의 운세' 게시글 URL을 찾습니다."""
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today_token = f"{now.month}월 {now.day}일"
    want = re.compile(rf"^{re.escape(TITLE_PREFIX)}\s*,\s*{re.escape(today_token)}\s*$")

    html = http_get(
        ASKJIYUN_TODAY_LIST_URL,
        headers=ASKJIYUN_HEADERS,
        timeout=(ASKJIYUN_CONNECT_TIMEOUT, ASKJIYUN_READ_TIMEOUT),
        retry=ASKJIYUN_RETRY,
    )
    soup = BeautifulSoup(html, "html.parser")

    candidates: list[tuple[str, str]] = []
    for a in soup.find_all("a"):
        title = _clean_title_text(a.get_text(" ", strip=True))
        href = a.get("href")
        if not href or "document_srl=" not in href:
            continue
        if TITLE_PREFIX not in title:
            continue
        post_url = urljoin(BASE, href)
        candidates.append((title, post_url))
        if want.search(title):
            return post_url

    # 폴백: 목록에서 가장 최신 '오늘의 운세' 링크를 사용
    if candidates:
        return candidates[0][1]
    raise RuntimeError("askjiyun.com 목록에서 '오늘의 운세' 게시글 링크를 찾지 못했습니다.")


def extract_mk_images(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    container_selectors = [
        "article",
        ".news_detail",
        ".article_body",
        ".news_cnt_detail_wrap",
        ".view_contents",
        "#container",
        "body",
    ]
    container = None
    for s in container_selectors:
        el = soup.select_one(s)
        if el:
            container = el
            break
    if not container:
        container = soup

    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        if src.startswith("data:"):
            continue
        abs_url = urljoin(base_url, src)
        if not re.search(r"\.(png|jpe?g|webp)(\?|$)", abs_url, re.I):
            continue
        if any(token in abs_url.lower() for token in ["logo", "icon", "sprite", "blank"]):
            continue
        candidates.append(abs_url)

    # 순서 유지 + 중복 제거
    seen = set()
    uniq = []
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    # 운세는 보통 2장 이미지로 구성됨
    return uniq[:4]


# ---------- 게시글 본문 파싱 (본문 컨테이너 후보를 넓게 잡음) ----------
def parse_post(html, debug=False):
    soup = BeautifulSoup(html, "html.parser")
    # 후보 클래스/셀렉터 여러개 시도
    selectors = [
        ".xe_content", ".read_body", "article", ".board_read .rd_body", ".read", "#content"
    ]
    candidates = []
    for s in selectors:
        el = soup.select_one(s)
        if el:
            candidates.append(el)
            if debug:
                logging.debug("selector matched: %s -> element text length=%d", s, len(el.get_text(" ", strip=True)))

    # fallback: 본문 길이 기준으로 가장 큰 요소를 선택
    if not candidates:
        # 전체 문서에서 텍스트가 많은 블록을 골라본다
        blocks = soup.find_all(['div', 'article', 'section', 'main'], limit=30)
        if blocks:
            candidates = blocks
            if debug:
                logging.debug("fallback blocks found: %d", len(blocks))

    if not candidates:
        # 마지막 수단: 전체 문서 텍스트
        text = soup.get_text("\n", strip=True)
        if debug:
            logging.debug("no candidates: using full document text length=%d", len(text))
        # 줄 단위로 정리: 각 라인을 strip하고 빈 줄은 연속 1개로 제한
        lines = [ln.strip() for ln in text.splitlines()]
        cleaned = []
        prev_blank = False
        for ln in lines:
            if ln == "":
                if not prev_blank:
                    cleaned.append("")
                    prev_blank = True
                else:
                    continue
            else:
                # 라인 내 연속 공백은 하나로 축소
                cleaned.append(re.sub(r"[ \t]{2,}", " ", ln))
                prev_blank = False
        text = "\n".join(cleaned).strip()
        return text

    # 가장 많은 텍스트를 가진 엘리먼트를 선택
    body = max(candidates, key=lambda el: len(el.get_text(" ", strip=True)))
    if debug:
        logging.debug("chosen body element text length=%d", len(body.get_text(" ", strip=True)))
    # 불필요한 요소(스크립트, 스타일, 공유/광고 블록 등) 제거
    for bad in body.select("script, style, noscript, iframe, header, footer, nav, form"):
        bad.decompose()
    for bad in body.select("[class*='share'], [class*='social'], [class*='ad'], [id*='share'], [id*='ad']"):
        bad.decompose()

    if debug:
        sample = body.get_text(" ", strip=True)[:500]
        logging.debug("before cleaning sample: %s", sample.replace("\n", "\\n"))

    # 텍스트로 변환한 뒤 라인 단위로 정리
    text = body.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = []
    prev_blank = False
    for ln in lines:
        if ln == "":
            if not prev_blank:
                cleaned.append("")
                prev_blank = True
            else:
                continue
        else:
            cleaned.append(re.sub(r"[ \t]{2,}", " ", ln))
            prev_blank = False
    # 이제 문장/단락 단위로 병합: 빈 줄은 단락 구분으로 유지
    paragraphs = []
    cur_lines = []
    for ln in cleaned:
        if ln == "":
            if cur_lines:
                paragraphs.append(" ".join(cur_lines).strip())
                cur_lines = []
            continue
        # 문장 내에서 강제 개행(문장 중간에 있는 경우)를 제거하고 이전 라인과 이어붙임
        # 단, 문장이 끝나는 경우(마침표/물음/감탄/%)에는 그대로 두어도 무방
        cur_lines.append(ln)

    if cur_lines:
        paragraphs.append(" ".join(cur_lines).strip())

    # 문장 연결 시 잘못된 공백/마침표 띄어쓰기 정리
    for i, p in enumerate(paragraphs):
        # 숫자와 뒤따르는 '년생' 같은 패턴의 잘못된 띄어쓰기 보정
        p = re.sub(r"\s+([.,:;?!%])", r"\1", p)
        p = re.sub(r"\s+년생", r"년생", p)
        # '운세지수\n93%.' 같이 잘려 있던 숫자 붙여쓰기 보정
        p = re.sub(r"\s+(\d+)%\s*\.", r" \1%.", p)
        paragraphs[i] = p

    # 보일러플레이트(예: '이 게시물을 ...') 제거: 짧은 단락에만 적용
    new_pars = []
    for p in paragraphs:
        if len(p) < 120 and re.search(r"이 게시물|공유|댓글|출처", p):
            if debug:
                logging.debug("dropping short boilerplate paragraph: %r", p[:120])
            continue
        new_pars.append(p)

    text = "\n\n".join(new_pars).strip()

    # 폴백: 정제 결과가 비어있다면 원문(body) 텍스트를 사용하여 손실을 막음
    if not text:
        if debug:
            logging.debug("cleaned text empty — falling back to raw body/document text")
        try:
            # 사용 가능한 body 변수를 사용하려 시도 (만약 존재하지 않으면 soup 전체 사용)
            raw_text = ''
            try:
                raw_text = body.get_text("\n", strip=True)
            except Exception:
                raw_text = soup.get_text("\n", strip=True)
            text = raw_text.strip()
            if debug:
                logging.debug("fallback raw text length=%d", len(text))
        except Exception as e:
            logging.exception("fallback to raw text failed: %s", e)
            text = ""
    if debug:
        logging.debug("final text length=%d", len(text))
        logging.debug("final sample: %s", text[:500].replace("\n", "\\n"))
    if not text:
        # 디버깅을 위해 원본 HTML을 저장
        try:
            path = os.path.join(os.getcwd(), "horoscope_debug_raw.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            logging.error("parse_post produced empty text; raw HTML saved to %s", path)
        except Exception as e:
            logging.exception("failed to save debug html: %s", e)
    return text


def send_to_gchat(
    message: str,
    *,
    title: Optional[str] = None,
    link_url: Optional[str] = None,
    image_urls: Optional[List[str]] = None,
    thread_key: Optional[str] = None,
):
    if not GCHAT_WEBHOOK:
        raise RuntimeError("환경변수 GCHAT_WEBHOOK이 설정되어 있지 않습니다.")
    image_urls = image_urls or []

    if not message and not image_urls and not title and not link_url:
        raise ValueError("전송할 내용이 없습니다 (message/image_urls/title/link_url 모두 비어있음).")

    # 기본은 text 메시지로 보내되, 이미지가 있으면 카드로 보냄.
    if image_urls:
        widgets = []
        if title or link_url:
            header_lines = []
            if title:
                header_lines.append(f"<b>{title}</b>")
            if link_url:
                header_lines.append(f"<a href=\"{link_url}\">{link_url}</a>")
            widgets.append({"textParagraph": {"text": "<br/>".join(header_lines)}})
        if message:
            widgets.append({"textParagraph": {"text": message.replace("\n", "<br/>")}})
        for u in image_urls:
            w = {"image": {"imageUrl": u}}
            if link_url:
                w["image"]["onClick"] = {"openLink": {"url": link_url}}
            widgets.append(w)
        payload = {"cards": [{"sections": [{"widgets": widgets}]}]}
    else:
        payload = {"text": message}
    if thread_key:
        payload["thread"] = {"threadKey": thread_key}
    query_params = {}
    if thread_key:
        query_params["threadKey"] = thread_key
        query_params["messageReplyOption"] = GCHAT_MESSAGE_REPLY_OPTION
    # 디버그용: 페이로드를 로깅 (실제 전송 전 확인 가능)
    # 메시지 메트릭 로깅: 길이와 개행 개수
    nl_count = message.count("\n")
    logging.info(
        "Sending to GChat: message length=%d chars, newlines=%d, title=%s, link=%s, images=%d",
        len(message),
        nl_count,
        bool(title),
        bool(link_url),
        len(image_urls),
    )
    logging.debug("GChat payload JSON: %s", payload)
    try:
        r = requests.post(GCHAT_WEBHOOK, params=query_params, json=payload, timeout=20)
    except Exception as e:
        logging.exception("GChat POST 실패: %s", e)
        raise
    if r.status_code >= 400:
        # 디버그를 위해 응답 본문 출력
        logging.error("GChat 전송 실패: %d %s", r.status_code, r.text)
    r.raise_for_status()
    logging.info("Google Chat 전송 성공: %d", r.status_code)


def _today_chat_thread() -> tuple[str, str]:
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    title = f"🔮 {today.month}/{today.day} 오늘의 운세"
    thread_key = GCHAT_THREAD_KEY or f"horoscope_{today.strftime('%Y%m%d')}"
    return title, thread_key


def _strip_trailing_boilerplate(text: str) -> str:
    """문서 끝의 보일러플레이트 문구(예: '이 게시물을 ...', '출처', '공유' 등)를 보수적으로 제거합니다.

    끝에서부터 연속으로 해당 패턴을 포함하는 라인을 제거합니다.
    """
    if not text:
        return text
    lines = text.rstrip().splitlines()
    # 한글 보일러플레이트 시작 패턴(여러 케이스 보수적으로 포함)
    pat = re.compile(r"^(이 게시물|이 글|출처|공유|댓글)", re.I)
    removed = False
    while lines and pat.search(lines[-1].strip()):
        lines.pop()
        removed = True
    if removed:
        # 남은 텍스트의 끝 공백 정리
        return "\n".join(lines).rstrip()

    # 보수적으로: 마지막 문장(텍스트 끝)에 보일러플레이트가 붙어있는 경우 제거
    # 예: "... 이 게시물을 공유합니다." 같은 형태
    tail_pattern = re.compile(r"(?:\s|^)(이 게시물을|이 글|출처|공유|댓글)[^\n]{0,200}\s*$", re.I)
    if tail_pattern.search(text):
        text = tail_pattern.sub("", text)
        return text.rstrip()

    return text


def _normalize_spacing(text: str) -> str:
    """원문에서 bs4가 남긴 남는 개행/공백을 보수적으로 정리합니다.

    - 같은 단어 사이에 끊어진 한 글자/숫자 라인은 앞뒤로 붙임
    - 문장부호 앞의 불필요한 공백 제거
    - '년생' 같은 패턴의 띄어쓰기 보정
    """
    if not text:
        return text

    # 1) 라인들을 가져와서 각 라인을 strip
    lines = [ln.strip() for ln in text.splitlines()]

    # 2) 짧은 라인(<=3문자)은 이전 라인에 붙이기 (숫자/단어 분리 방지)
    merged = []
    for ln in lines:
        if ln == "":
            merged.append("")
            continue
        if len(ln) <= 3 and merged and merged[-1] != "":
            # 공백 필요 시 하나만 삽입
            if not merged[-1].endswith(" "):
                merged[-1] = merged[-1] + " " + ln
            else:
                merged[-1] = merged[-1] + ln
        else:
            merged.append(ln)

    text2 = "\n".join(merged)

    # 3) 여러줄로 나뉜 문장 내부의 개행을 공백으로 바꿔 문장 연결
    #    단락 구분(빈 줄)은 유지
    paragraphs = []
    cur = []
    for ln in text2.splitlines():
        if ln.strip() == "":
            if cur:
                paragraphs.append(" ".join(cur))
                cur = []
            continue
        cur.append(ln.strip())
    if cur:
        paragraphs.append(" ".join(cur))

    out = "\n\n".join(paragraphs)

    # 4) 문장부호 앞 공백 제거, 마침표 후 하나의 공백 유지
    out = re.sub(r"\s+([.,:;?!%])", r"\1", out)
    out = re.sub(r"\.\s*([A-Za-z0-9가-힣])", r". \1", out)

    # 5) '년생' 등에서 불필요한 공백 제거
    out = re.sub(r"(\d)\s*,\s*(\d)", r"\1, \2", out)
    out = re.sub(r"(\d)\s+년생", r"\1년생", out)
    out = re.sub(r"(\d+)\s*월\s*(\d+)\s*일", r"\1월 \2일", out)

    # 6) 괄호, 꺽쇠 주변 공백 정리
    out = re.sub(r"\s*\(\s*", " (", out)
    out = re.sub(r"\s*\)\s*", ") ", out)
    out = re.sub(r"\s*〈\s*", " 〈", out)
    out = re.sub(r"\s*〉\s*", "〉 ", out)

    # 마지막 공백/중복 공백 정리
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

def _format_jiyun_readable(text: str) -> str:
    """askjiyun 본문을 모바일에서 읽기 좋게 단락을 나눕니다."""
    if not text:
        return text
    t = text.strip()
    # 띠 구분(〈쥐띠〉 등) 앞에 단락을 넣어 가독성 개선
    t = re.sub(r"\s*(〈[^〉]+〉)", r"\n\n\1", t)
    # 운세지수(…%)를 각 띠의 끝으로 보이게
    t = re.sub(r"\s*(운세지수\s*\d+%\.?)\s*", r" \1\n", t)
    # 과도한 빈 줄 정리
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------- main ----------
def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="매일경제 오늘의 운세 전송기 (개인용)")
    parser.add_argument("--dry-run", action="store_true", help="웹훅으로 전송하지 않고 결과를 콘솔에 출력합니다.")
    parser.add_argument("--debug", action="store_true", help="디버그 로깅을 활성화")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--mk-only", action="store_true", help="매일경제 운세만 전송/출력합니다.")
    src.add_argument("--jiyun-only", action="store_true", help="askjiyun.com 운세만 전송/출력합니다.")
    # keep only the essential flags
    args = parser.parse_args(argv)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    which = "both"
    if args.mk_only:
        which = "mk"
    elif args.jiyun_only:
        which = "jiyun"
    logging.info("시작: 오늘의 운세 전송 (%s)", which)

    # robots 체크 (선택) — 개인용이라면 실패 시에도 계속 진행하도록 True 반환
    if which in ("both", "mk") and not allowed_by_robots(SEARCH_URL, MK_BASE):
        logging.warning("robots.txt에서 크롤링을 금지했을 가능성이 있습니다. 계속 진행하려면 코드를 수정하세요.")
    if which in ("both", "jiyun") and not allowed_by_robots(ASKJIYUN_TODAY_LIST_URL, BASE):
        logging.warning("robots.txt에서 크롤링을 금지했을 가능성이 있습니다. 계속 진행하려면 코드를 수정하세요.")

    jobs = []
    mk_job = None
    jiyun_job = None

    if which in ("both", "mk"):
        post_url = find_today_post_url()
        logging.info("MK 게시글 URL: %s", post_url)

        html = http_get(post_url, headers=MK_HEADERS, timeout=MK_TIMEOUT, retry=MK_RETRY)
        soup = BeautifulSoup(html, "html.parser")

        page_title = None
        og = soup.select_one('meta[property="og:title"]')
        if og and og.get("content"):
            page_title = og.get("content").strip()
        if not page_title:
            page_title = (soup.find("title").get_text(strip=True) if soup.find("title") else TITLE_PREFIX)

        # 주말 합본 글(제목에 날짜 2개) 처리
        now = datetime.now(ZoneInfo("Asia/Seoul")).date()
        title_dates = _extract_dates_from_title(page_title)
        if SKIP_MK_WEEKEND_DUPLICATE and len(title_dates) >= 2 and now in title_dates and now != title_dates[0]:
            logging.info(
                "MK 주말 합본 글로 판단되어 중복 전송 방지로 건너뜀: title_dates=%s, today=%s",
                title_dates,
                now,
            )
            mk_job = None
        else:
            image_urls = extract_mk_images(html, post_url)
            # 본문이 이미지로만 구성되기도 함(MK는 자주 이미지 2장). 이 경우 텍스트 파싱은 잡음이 많아 제외.
            if image_urls:
                message = ""
            else:
                mk_text = parse_post(html)
                mk_text = _normalize_spacing(_strip_trailing_boilerplate(mk_text))
                message = f"🔮 {page_title}\n{post_url}\n\n{mk_text}".strip()
            mk_job = {
                "title": page_title,
                "url": post_url,
                "message": message,
                "image_urls": image_urls,
            }

    if which in ("both", "jiyun"):
        try:
            post_url = find_askjiyun_today_post_url()
            logging.info("askjiyun 게시글 URL: %s", post_url)

            html = http_get(
                post_url,
                headers=ASKJIYUN_HEADERS,
                timeout=(ASKJIYUN_CONNECT_TIMEOUT, ASKJIYUN_READ_TIMEOUT),
                retry=ASKJIYUN_RETRY,
            )
            soup = BeautifulSoup(html, "html.parser")
            page_title = (soup.find("title").get_text(strip=True) if soup.find("title") else "askjiyun 오늘의 운세")
            # 본문 파싱/정리
            text = parse_post(html)
            text = _normalize_spacing(_strip_trailing_boilerplate(text))
            text = _format_jiyun_readable(text)
            message = f"🔮 {page_title}\n{post_url}\n\n{text}".strip()
            jiyun_job = {
                "title": page_title,
                "url": post_url,
                "message": message,
                "image_urls": [],
            }
        except Exception as e:
            # Actions 등에서 간헐적으로 타임아웃이 나면 MK만이라도 보내도록 한다.
            logging.warning("askjiyun 가져오기 실패(건너뜀): %s", e)
            jiyun_job = None

    # both 모드: MK(이미지) + 지윤(텍스트)을 한 번에 보기 좋게 하나의 카드/메시지로 합친다.
    if which == "both" and mk_job and jiyun_job:
        combined_title = "오늘의 운세"
        combined_url = None
        jiyun_body = jiyun_job["message"]
        if "\n\n" in jiyun_body:
            jiyun_body = jiyun_body.split("\n\n", 1)[1]
        # 지윤 본문 첫 줄(게시글 헤더)을 카드 제목으로 쓰고, 본문에서는 제거
        j_lines = jiyun_body.splitlines()
        header_idx = None
        for i, ln in enumerate(j_lines):
            if ln.strip():
                header_idx = i
                header = ln.strip()
                combined_title = header if header.startswith("🔮") else f"🔮 {header}"
                break
        body_lines = j_lines[header_idx + 1 :] if header_idx is not None else j_lines
        # 시작 부분의 공백 줄 제거
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        combined_message = "\n".join(body_lines).strip()
        jobs.append(
            {
                "title": combined_title,
                "url": combined_url,
                "message": combined_message,
                "image_urls": mk_job["image_urls"],
                "is_combined": True,
            }
        )
    else:
        if mk_job:
            jobs.append(mk_job)
        if jiyun_job:
            jobs.append(jiyun_job)

    if not jobs:
        logging.warning("전송할 job이 없습니다. (MK=%s, askjiyun=%s)", bool(mk_job), bool(jiyun_job))
        return

    # 길이 제한 처리(각 메시지별)
    for j in jobs:
        if len(j["message"]) > MAX_MESSAGE_LEN:
            logging.warning("메시지가 너무 깁니다 (%d자). 자릅니다.", len(j["message"]))
            j["message"] = j["message"][:MAX_MESSAGE_LEN] + "\n\n(메시지가 길어 일부만 전송됩니다. 원문에서 전체 확인하세요.)"

    if args.dry_run:
        logging.info("Dry-run: 웹훅 전송을 건너뜁니다. 출력으로 대신합니다.")
        thread_title, thread_key = _today_chat_thread()
        print(f"[thread title] {thread_title}")
        print(f"[thread key] {thread_key}")
        print()
        for j in jobs:
            if j["image_urls"]:
                if j.get("is_combined"):
                    print(f"{j['title']}")
                else:
                    print(f"🔮 {j['title']}\n{j['url']}")
                if j["message"]:
                    print()
                    print(j["message"])
                print("\n[images]")
                for u in j["image_urls"]:
                    print(u)
                print()
            else:
                print(j["message"])
                print()
    else:
        thread_title, thread_key = _today_chat_thread()
        send_to_gchat(thread_title, thread_key=thread_key)
        time.sleep(1.5)
        for j in jobs:
            send_to_gchat(
                j["message"],
                title=j["title"],
                link_url=j.get("url"),
                image_urls=j["image_urls"],
                thread_key=thread_key,
            )
        logging.info("완료.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("실행 중 에러 발생: %s", e)
        raise
