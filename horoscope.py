# -*- coding: utf-8 -*-
"""
askjiyun.com/today의 '오늘의 운세, M월 D일' 게시글 전체 본문을
Google Chat Incoming Webhook으로 전송하는 스크립트 (개인용).

설치: pip install requests beautifulsoup4
환경변수: GCHAT_WEBHOOK  (Google Chat에서 발급받은 웹훅 URL)
"""
import os
import re
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import urllib.robotparser as robotparser

# ---------- 설정 ----------
BASE = "https://askjiyun.com"
LIST_URL = urljoin(BASE, "today")
TITLE_RE = re.compile(r"^오늘의 운세,\s*\d+월\s*\d+일")
GCHAT_WEBHOOK = os.getenv("GCHAT_WEBHOOK")

# 전송 최대 길이: 너무 길면 웹훅/채널에서 문제될 수 있으므로 안전하게 자름
MAX_MESSAGE_LEN = 14000

# HTTP 헤더 (간단한 브라우저처럼)
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/127.0.0.0 Safari/537.36"),
    "Accept-Language": "ko,en;q=0.8",
    "Referer": BASE,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------- 유틸: robots 체크 ----------
def allowed_by_robots(url, user_agent="*"):
    try:
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(BASE, "/robots.txt")
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        logging.warning("robots.txt 체크 실패: %s (계속 진행)", e)
        # robots 체크 실패 시에도 개인용으로 계속 진행하겠다면 True 반환
        return True


# ---------- HTTP 요청 with retry ----------
def http_get(url, timeout=15, retry=3, backoff=1.2):
    last_exc = None
    for i in range(1, retry + 1):
        try:
            resp = requests.get(url, headers=UA, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exc = e
            logging.warning("GET 실패 (%s) %s (시도 %d/%d)", url, e, i, retry)
            time.sleep(backoff)
    raise RuntimeError(f"GET 실패: {url} / {last_exc}")


# ---------- 목록에서 오늘 게시글 링크 찾기 ----------
def find_today_post_url():
    html = http_get(LIST_URL)
    soup = BeautifulSoup(html, "html.parser")

    # 1) 제목 텍스트가 정확히 매칭되는 a 태그 찾기
    anchors = soup.find_all("a", string=re.compile(r"^오늘의 운세,\s*\d+월\s*\d+일"))
    if not anchors:
        # 2) 텍스트 조합(공백/nbsp 등) 보정해서 찾기
        anchors = []
        for a in soup.find_all("a"):
            txt = (a.get_text(" ", strip=True) or "").replace("\u00a0", " ")
            if re.match(r"^오늘의 운세,\s*\d+월\s*\d+일", txt):
                anchors.append(a)

    if not anchors:
        # 3) 대체: 모든 링크에서 접두사로 시작하는 텍스트 검사
        for a in soup.select("a"):
            txt = (a.get_text(" ", strip=True) or "")
            if txt.startswith("오늘의 운세,"):
                anchors.append(a)

    if not anchors:
        raise RuntimeError("목록에서 '오늘의 운세' 링크를 찾지 못했습니다.")

    # 오늘 날짜 우선 탐색
    # use KST (Asia/Seoul) so runs on servers in UTC still select the same "오늘" as Korea
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    md = f"{now.month}월 {now.day}일"
    for a in anchors:
        txt = a.get_text(" ", strip=True)
        if md in txt:
            href = a.get("href")
            return urljoin(BASE, href)

    # 없으면 가장 최근(첫 항목)
    href = anchors[0].get("href")
    return urljoin(BASE, href)


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


# ---------- Google Chat 전송 ----------
def send_to_gchat(message, use_card=False):
    if not GCHAT_WEBHOOK:
        raise RuntimeError("환경변수 GCHAT_WEBHOOK이 설정되어 있지 않습니다.")
    if use_card:
        # Google Chat card payload keeps text formatting in a textParagraph
        payload = {"cards": [{"sections": [{"widgets": [{"textParagraph": {"text": message}}]}]}]}
    else:
        payload = {"text": message}
    # 디버그용: 페이로드를 로깅 (실제 전송 전 확인 가능)
    # 메시지 메트릭 로깅: 길이와 개행 개수
    nl_count = message.count("\n")
    logging.info("Sending to GChat: message length=%d chars, newlines=%d", len(message), nl_count)
    logging.debug("GChat payload JSON: %s", payload)
    try:
        r = requests.post(GCHAT_WEBHOOK, json=payload, timeout=20)
    except Exception as e:
        logging.exception("GChat POST 실패: %s", e)
        raise
    if r.status_code >= 400:
        # 디버그를 위해 응답 본문 출력
        logging.error("GChat 전송 실패: %d %s", r.status_code, r.text)
    r.raise_for_status()
    logging.info("Google Chat 전송 성공: %d", r.status_code)


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

    # 6) 괄호, 꺽쇠 주변 공백 정리
    out = re.sub(r"\s*\(\s*", " (", out)
    out = re.sub(r"\s*\)\s*", ") ", out)
    out = re.sub(r"\s*〈\s*", " 〈", out)
    out = re.sub(r"\s*〉\s*", "〉 ", out)

    # 마지막 공백/중복 공백 정리
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ---------- main ----------
def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="askjiyun 오늘의 운세 전송기 (개인용)")
    parser.add_argument("--dry-run", action="store_true", help="웹훅으로 전송하지 않고 결과를 콘솔에 출력합니다.")
    parser.add_argument("--debug", action="store_true", help="디버그 로깅을 활성화")
    # keep only the essential flags
    args = parser.parse_args(argv)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    logging.info("시작: askjiyun 오늘의 운세 전송 (전체본문)")

    # robots 체크 (선택) — 개인용이라면 실패 시에도 계속 진행하도록 True 반환
    if not allowed_by_robots(LIST_URL):
        logging.warning("robots.txt에서 크롤링을 금지했을 가능성이 있습니다. 계속 진행하려면 코드를 수정하세요.")
        # 계속 진행하려면 주석처리하거나 허용으로 변경하세요.
        # return

    post_url = find_today_post_url()
    logging.info("찾은 게시글 URL: %s", post_url)

    # 게시글 요청/파싱
    html = http_get(post_url)
    # 기본 동작: 정제된 본문(parse_post)을 사용
    text = parse_post(html, debug=args.debug)
    if args.debug:
        logging.debug("default(clean) mode: cleaned text length=%d", len(text))

    # 최소 변환 요구사항만 적용:
    # 1) '[' 이전 문자 제거 (본문 시작에서 대괄호 전까지 제거)
    # 2) '<' '>' 및 '〈' '〉' 전후로 줄바꿈 2회
    # 3) '년생' 뒤에 줄바꿈
    # 4) '.' 뒤에 줄바꿈
    # (그 외 기존의 추가 정제 동작은 하지 않음)

    # 1) '[' 이전 문자 제거: 첫 '['가 등장하기 전의 모든 텍스트를 제거
    try:
        idx = text.find('[')
        if idx != -1:
            text = text[idx:]
    except Exception:
        pass

    # 제목(페이지 <title>에서 가져오기)
    try:
        soup = BeautifulSoup(html, "html.parser")
        title_raw = (soup.find("title").get_text(strip=True) if soup.find("title") else "")
        # 사이트 도메인 등 불필요한 접미사/접두사 제거
        netloc = urlparse(BASE).netloc
        page_title = title_raw
        if netloc:
            # remove occurrences of the domain surrounded by common separators
            page_title = re.sub(rf"\s*[-–—|·:]?\s*{re.escape(netloc)}\s*$", "", page_title)
            page_title = re.sub(rf"^{re.escape(netloc)}\s*[-–—|·:]?\s*", "", page_title)
            page_title = page_title.replace(netloc, "")
        # strip leftover separators/extra whitespace
        page_title = re.sub(r"^[\s\-–—|·:]+|[\s\-–—|·:]+$", "", page_title).strip()
    except Exception:
        page_title = "오늘의 운세"

    header = f"🔮 *{page_title}*\n\n"
    message = header + text

    # 길이 제한 처리
    if len(message) > MAX_MESSAGE_LEN:
        logging.warning("메시지가 너무 깁니다 (%d자). 자릅니다.", len(message))
        message = message[:MAX_MESSAGE_LEN] + "\n\n(메시지가 길어 일부만 전송됩니다. 원문에서 전체 확인하세요.)"

    # 이제 요청된 최소 후처리만 수행
    # 2) 브래킷 전후로 빈 줄(두 줄) 삽입: ASCII < > 와 fullwidth 〈 〉 처리
    message = re.sub(r"\s*(<|〈)", r"\n\n\1", message)
    message = re.sub(r"(>|〉)\s*", r"\1\n\n", message)
    # 연속 개행은 최대 2개로 제한
    message = re.sub(r"\n{3,}", "\n\n", message)

    # 3) '년생' 뒤 줄바꿈
    message = re.sub(r"(년생)\s*", r"\1\n", message)

    # 4) 마침표 뒤 줄바꿈
    message = re.sub(r"\.\s*", ".\n", message)

    # 마지막: 보수적으로 본문 끝의 잔여 보일러플레이트(예: '이 게시물을.') 제거
    # 끝에서부터 3줄 정도를 검사하여 '이 게시물' 같은 패턴이 포함된 라인을 제거
    try:
        # 라인 단위로 뒤쪽에서부터 검사하여 다음을 제거:
        # - 온전히 '.' 또는 공백으로만 된 라인들
        # - 마지막 라인에 붙어 있는 '이 게시물을.' 등 보일러플레이트 문구
        lines = message.rstrip().splitlines()
        # 1) remove trailing lines that are only dots/spaces
        while lines and re.fullmatch(r"[.\s]+", lines[-1]):
            lines.pop()

        # 2) if the last line ends with boilerplate phrase, strip that phrase
        if lines:
            last = lines[-1]
            new_last = re.sub(r"(?:\s|^)(?:이 게시물을?|이 게시물|이 글|출처|공유|댓글)[\s\.:,]*$", "", last, flags=re.I).rstrip()
            lines[-1] = new_last

        # 3) if after stripping the last line becomes empty or is solely a boilerplate, pop it
        while lines and re.fullmatch(r"\s*", lines[-1]):
            lines.pop()

        message = "\n".join(lines).rstrip()
    except Exception:
        pass

    if args.dry_run:
        # dry-run: 웹훅 전송을 하지 않고 출력
        logging.info("Dry-run: 웹훅 전송을 건너뜁니다. 출력으로 대신합니다.")
        print(message)
    else:
        send_to_gchat(message)
        logging.info("완료.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("실행 중 에러 발생: %s", e)
        raise
