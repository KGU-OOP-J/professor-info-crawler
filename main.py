"""
경기대학교 컴퓨터공학전공 교수 목록, 사진, 상세 이력 수집기

설치: pip install curl_cffi lxml
실시간: python kgu_professor_crawler.py
HAR 검증 (오프라인): python kgu_professor_crawler.py --har download
"""

import argparse
import base64
import copy
import json
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from lxml import html

HOMEPAGE_URL = "https://www.kyonggi.ac.kr/u_computer/contents.do?key=2159"
LIST_URL = "https://www.kyonggi.ac.kr/common/deco/professor_list.jsp?dept_code=0A551&view_mode="

AJAX_HEADERS = {
    "Accept": "text/html, */*; q=0.01",
    "Referer": HOMEPAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}
IMAGE_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": HOMEPAGE_URL,
}


def has_class(class_name: str) -> str:
    return f'contains(concat(" ", normalize-space(@class), " "), " {class_name} ")'


CARD_XPATH = f'//div[{has_class("employee_item")}]'
NAME_XPATH = f'.//div[{has_class("name_wrap")}]/span[{has_class("name")}]'
TITLE_XPATH = f'.//div[{has_class("name_wrap")}]/span[{has_class("employee")}]'
IMAGE_XPATH = f'.//div[{has_class("employee_img")}]//img'
DETAIL_TAB_XPATH = f'//div[{has_class("profile_tab_box")}]'

DETAIL_FIELDS = (
    "career",
    "international_publications",
    "domestic_publications",
    "books",
    "external_research_funding",
)


def profile_xpath(kind: str) -> str:
    return (
        f'.//div[{has_class("profile_item")} and {has_class(kind)}]'
        f'/span[{has_class("text")}]'
    )


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def get_text(element, xpath: str) -> str | None:
    found = element.xpath(xpath)
    if not found:
        return None
    value = clean_text(found[0].text_content())
    return value if value and value != "--" else None


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "unknown"


def professor_category(title: str) -> str:
    if title in ("교수", "부교수", "조교수"):
        return "전임교원"
    if title == "명예교수":
        return "명예교수"
    return "비전임교원"


def image_extension(blob: bytes) -> str:
    # 이 사이트는 JPEG를 image/gif로 반환하기도 하므로 magic bytes 확인.
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return ".webp"
    if blob.startswith(b"BM"):
        return ".bmp"
    raise ValueError("이미지 magic bytes를 인식할 수 없습니다")


def detail_url_from_card(card) -> str | None:
    href = card.xpath(f'.//a[{has_class("detail_view")}]/@href')
    if href:
        return urljoin(LIST_URL, href[0])
    euid = card.xpath('.//*[@data-euid]/@data-euid')
    if euid:
        # data-euid는 이미 URL 인코딩된 문자열이므로 재인코딩하지 않는다.
        return urljoin(LIST_URL, f"./professor_detail.jsp?euid={euid[0]}")
    return None


class LiveSource:
    def __init__(self):
        try:
            from curl_cffi import requests
        except ImportError as e:
            raise RuntimeError("pip install curl_cffi lxml 명령으로 의존성을 설치하세요") from e
        self.session = requests.Session(impersonate="chrome")

    def __enter__(self):
        # HAR과 동일하게 메인 페이지 방문 후 AJAX 요청 수행.
        response = self.session.get(HOMEPAGE_URL, timeout=30)
        response.raise_for_status()
        return self

    def __exit__(self, *_):
        self.session.close()

    def _get(self, url, headers):
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                return response.content
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"요청 실패: {url} ({last_error})")

    def list_html(self) -> bytes:
        return self._get(LIST_URL, AJAX_HEADERS)

    def detail_html(self, url: str) -> bytes:
        return self._get(url, AJAX_HEADERS)

    def photo(self, url: str) -> bytes:
        return self._get(url, IMAGE_HEADERS)


class HarSource:
    """브라우저에서 저장된 실제 응답 본문을 재생. 네트워크 사용 안 함."""

    def __init__(self, path: Path):
        with path.open("r", encoding="utf-8-sig") as file:
            entries = json.load(file)["log"]["entries"]
        self.entries = {e["request"]["url"]: e for e in entries}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def _content(self, url: str) -> bytes:
        entry = self.entries.get(url)
        if entry is None:
            raise LookupError(f"HAR에 해당 응답이 기록되어 있지 않습니다: {url}")
        response = entry["response"]
        if not 200 <= response.get("status", 0) < 300:
            raise RuntimeError(f"HAR에 실패한 응답이 기록되어 있습니다: {url}")
        body = response.get("content", {})
        data = body.get("text")
        if data is None:
            raise ValueError(f"HAR에 응답 본문이 저장되어 있지 않습니다: {url}")
        if body.get("encoding") == "base64":
            return base64.b64decode(data)
        return data.encode("utf-8")

    def list_html(self) -> bytes:
        return self._content(LIST_URL)

    def detail_html(self, url: str) -> bytes:
        return self._content(url)

    def photo(self, url: str) -> bytes:
        return self._content(url)


def career_from_tab(tab) -> list[dict]:
    """경력사항 탭의 계층을 보존: 학력/경력 등 제목과 세부 항목."""
    career = []
    for li in tab.xpath('./ul/li | ./ol/li'):
        nested = li.xpath('./ul/li | ./ol/li')
        if nested:
            # 예) <li>학력<ul><li>...</li></ul></li>
            heading = clean_text(" ".join(li.xpath('./text()')))
            items = [clean_text(x.text_content()) for x in nested]
            items = [x for x in items if x]
            career.append({"section": heading or None, "items": items})
        else:
            value = clean_text(li.text_content())
            if value:
                career.append({"section": None, "items": [value]})
    # 목록이 없는 예외적인 응답도 손실 없이 보존.
    if not career:
        text = clean_text(tab.text_content())
        if text:
            career.append({"section": None, "items": [text]})
    return career


def numbered_items_from_tab(tab) -> list[str]:
    result = []
    for li in tab.xpath('./ul/li | ./ol/li'):
        # 숫자 <span class="count">는 본문 내용이 아니므로 제거한다.
        item = copy.deepcopy(li)
        for number in item.xpath(f'.//span[{has_class("count")}]'):
            number.drop_tree()
        value = clean_text(item.text_content())
        if value:
            result.append(value)
    # 리스트 아닌 텍스트/문단 응답도 수집.
    if not result:
        for p in tab.xpath('./p | ./div'):
            value = clean_text(p.text_content())
            if value:
                result.append(value)
    return result


def parse_detail(response_body: bytes) -> dict:
    document = html.fromstring(response_body.decode("utf-8", errors="replace"))
    profile = document.xpath(f'//div[{has_class("employee_wrap")}]')
    
    if not profile:
        raise ValueError("상세정보 HTML에서 employee_wrap을 찾지 못했습니다")

    fields = {
        "department": get_text(document, profile_xpath("belong")),
        "position": get_text(document, profile_xpath("affiliation")),
        "phone": get_text(document, profile_xpath("tel")),
        "email": get_text(document, profile_xpath("email")),
    }

    tabs = document.xpath(DETAIL_TAB_XPATH)
    
    if not tabs:
        raise ValueError("상세정보 HTML에서 profile_tab_box를 찾지 못했습니다")

    # 버튼 순서는 HAR에서 확인한 경력/국제/국내/저서/교외연구비 순서
    buttons = [clean_text(b.text_content()) for b in document.xpath(f'//div[{has_class("profile_tab")}]/button[{has_class("profile_btn")}]')]
    mapped = {}
    if len(buttons) == len(tabs):
        for button, tab in zip(buttons, tabs):
            norm = button.replace("ㆍ", "·").replace(" ", "")
            if "경력" in norm:
                mapped["career"] = career_from_tab(tab)
            elif "국제논문" in norm:
                mapped["international_publications"] = numbered_items_from_tab(tab)
            elif "국내논문" in norm:
                mapped["domestic_publications"] = numbered_items_from_tab(tab)
            elif "저서" in norm:
                mapped["books"] = numbered_items_from_tab(tab)
            elif "교외연구비" in norm:
                mapped["external_research_funding"] = numbered_items_from_tab(tab)
    # 설명문구가 없는 구형 응답도 5개 탭이 있으면 순서에 맞춰 처리.
    if len(tabs) == 5:
        for key, tab in zip(DETAIL_FIELDS, tabs):
            mapped.setdefault(key, career_from_tab(tab) if key == "career" else numbered_items_from_tab(tab))
    elif len(mapped) != len(DETAIL_FIELDS):
        raise ValueError(f"예상하지 못한 상세정보 탭 구조: {len(tabs)}개, 라벨={buttons}")

    fields.update(mapped)
    return fields


def scrape(source, output_dir: Path, *, download_images=True, delay=0.2):
    document = html.fromstring(source.list_html().decode("utf-8", errors="replace"))
    cards = document.xpath(CARD_XPATH)
    if not cards:
        raise RuntimeError("교수 목록이 비어 있습니다. AJAX professor_list.jsp 응답을 확인하세요.")

    output_dir.mkdir(parents=True, exist_ok=True)
    if download_images:
        (output_dir / "images").mkdir(exist_ok=True)

    records = []
    categories = Counter()
    statuses = Counter()
    image_success = 0
    image_failed = 0

    for index, card in enumerate(cards, start=1):
        name = get_text(card, NAME_XPATH)
        title = get_text(card, TITLE_XPATH)
        if not name or not title:
            raise ValueError(f"{index}번째 카드의 이름/직함이 없습니다")

        category = professor_category(title)
        categories[category] += 1
        filename = f"{index:04d}-{safe_filename(name)}"
        detail_url = detail_url_from_card(card)
        image_nodes = card.xpath(IMAGE_XPATH)
        photo_url = None
        alt = name
        if image_nodes:
            photo_url = image_nodes[0].get("src") or image_nodes[0].get("data-src")
            photo_url = urljoin(LIST_URL, photo_url) if photo_url else None
            alt = image_nodes[0].get("alt") or name

        record = {
            "order": index,
            "category": category,
            "category_order": categories[category],
            "name": name,
            "title": title,
            "department": get_text(card, profile_xpath("belong")),
            "position": get_text(card, profile_xpath("affiliation")),
            "phone": get_text(card, profile_xpath("tel")),
            "email": get_text(card, profile_xpath("email")),
            "career": None,
            "international_publications": None,
            "domestic_publications": None,
            "books": None,
            "external_research_funding": None,
            "detail_status": "not_requested",
            "detail_error": None,
            "image": None,
            "image_url": photo_url,
            "detail_url": detail_url,
        }

        if detail_url:
            try:
                parsed = parse_detail(source.detail_html(detail_url))
                for key, value in parsed.items():
                    # 상세 페이지의 소속/직책이 목록보다 자세한 경우가 있다.
                    if key in ("department", "position", "phone", "email"):
                        if value is not None:
                            record[key] = value
                    else:
                        record[key] = value
                record["detail_status"] = "ok"
            except LookupError as e:
                record["detail_status"] = "missing_from_har"
                record["detail_error"] = str(e)
            except Exception as e:
                record["detail_status"] = "error"
                record["detail_error"] = str(e)
                print(f"[WARN] {filename}: 상세정보 수집 실패 - {e}")
        else:
            record["detail_status"] = "no_detail_url"
        statuses[record["detail_status"]] += 1

        if download_images and photo_url:
            try:
                blob = source.photo(photo_url)
                if not blob:
                    raise ValueError("빈 이미지 응답")
                image_file = output_dir / "images" / (
                    f"{index:04d}-{safe_filename(alt)}{image_extension(blob)}"
                )
                image_file.write_bytes(blob)
                record["image"] = image_file.relative_to(output_dir).as_posix()
                image_success += 1

            except Exception as e:
                image_failed += 1
                print(f"[WARN] {filename}: 사진 실패 - {e}")

        (output_dir / f"{filename}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        records.append(record)
        
        career = record["career"]
        intl = record["international_publications"]
        domestic = record["domestic_publications"]
        books = record["books"]
        research = record["external_research_funding"]

        assert isinstance(career, list)
        assert isinstance(intl, list)
        assert isinstance(domestic, list)
        assert isinstance(books, list)
        assert isinstance(research, list)

        counts = (
            f"career={sum(len(group['items']) for group in career)}, "
            f"intl={len(intl)}, "
            f"domestic={len(domestic)}, "
            f"books={len(books)}, "
            f"research={len(research)}"
        )
        
        print(f"[{index:04d}/{len(cards):04d}] {name} ({category}) "
              f"detail={record['detail_status']} {counts}")
        
        if isinstance(source, LiveSource) and index < len(cards) and delay:
            time.sleep(delay)

    summary = {
        "source": LIST_URL,
        "total": len(records),
        "counts": dict(categories),
        "detail_status_counts": dict(statuses),
        "image_success": image_success,
        "image_failures": image_failed,
        "professors": records,
    }
    (output_dir / "professors.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n완료: 교수 {len(records)}명 / 상세정보 상태 {dict(statuses)} ")
    print(f"사진 저장 {image_success}명 / 실패 {image_failed}명")
    print(f"출력: {output_dir.resolve()}")
    if statuses["missing_from_har"]:
        print("주의: HAR에 없는 상세정보는 null입니다. 전체 상세정보는 HAR 없이 실시간 실행하세요.")
    return summary


def main():
    parser = argparse.ArgumentParser(description="경기대학교 컴퓨터공학전공 교수 상세정보 수집기")
    
    parser.add_argument("--har", type=Path, help="HAR 응답만 읽는 오프라인 검증 모드")
    parser.add_argument("--output", type=Path, default=Path("output"), help="저장 폴더")
    parser.add_argument("--no-images", action="store_true", help="이미지 다운로드 생략")
    parser.add_argument("--delay", type=float, default=0.2, help="실시간 요청 사이 지연(초)")
    
    args = parser.parse_args()
    
    if args.delay < 0:
        parser.error("--delay는 음수일 수 없습니다")
        
    provider = HarSource(args.har) if args.har else LiveSource()
    
    with provider as source:
        result = scrape(source, args.output, download_images=not args.no_images, delay=args.delay)
        
    if not args.har and result["detail_status_counts"].get("ok", 0) != result["total"]:
        raise SystemExit("오류: 일부 교수 상세정보 수집에 실패했습니다. JSON의 detail_status/detail_error를 확인하세요.")


if __name__ == "__main__":
    main()
