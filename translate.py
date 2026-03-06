#!/usr/bin/env python3
"""
Paperturn Batch Translation Script
Scrapes English pages, translates via Claude API (translate + copywrite), outputs .xlsx files.
"""

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://www.paperturn.com"
MODEL = "claude-sonnet-4-6"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "Translation Project" / "output"

TRANSLATOR_SYSTEM = (
    "You are an expert translator specializing in {lang}. "
    "Translate the following marketing content from English to {lang}. "
    "Preserve the exact meaning, tone, and intent. Do not add or remove information. "
    "CRITICAL: Keep the labels/tags in English exactly as they appear (e.g. 'H1:', 'P:', "
    "'CTA Button:', 'Page Title:', 'Meta Description:', 'URL Slug:'). "
    "Only translate the text AFTER the colon. "
    "Return the translation in the exact same structured format, one item per line."
)

COPYWRITER_SYSTEM = (
    "You are one of the world's leading copywriters and a native {lang} speaker "
    "who creates exceptional marketing copy. You have received a translation from English. "
    "Your job is to polish this into compelling, natural-sounding {lang} marketing copy "
    "that reads as if it were originally written in {lang}. "
    "Preserve the meaning and structure. "
    "CRITICAL: Keep the labels/tags in English exactly as they appear (e.g. 'H1:', 'P:', "
    "'CTA Button:', 'Page Title:', 'Meta Description:', 'URL Slug:'). "
    "Only modify the text AFTER the colon. Make it persuasive, punchy, and professional. "
    "IMPORTANT: Use a maximum of 1 em dash (—) across ALL the copy combined. Only one single em dash "
    "is allowed in the entire output. For everything else, use commas, periods, colons, or other punctuation. "
    "Return the result in the exact same structured format, one item per line."
)

SEO_TRANSLATOR_SYSTEM = (
    "You are an expert SEO translator specializing in {lang}. "
    "Translate the following SEO metadata from English to {lang}. "
    "For the URL slug, create a short, SEO-friendly slug in {lang} using only "
    "lowercase letters, numbers, and hyphens. "
    "CRITICAL: Keep the labels in English exactly as they appear "
    "(e.g. 'Page Title:', 'Meta Description:', 'URL Slug:'). "
    "Only translate the text AFTER the colon. Return in the same structured format."
)

SEO_COPYWRITER_SYSTEM = (
    "You are a world-class SEO copywriter and native {lang} speaker. "
    "Polish the following translated SEO metadata into compelling, natural {lang} copy "
    "optimized for search engines. The URL slug should be concise and keyword-rich in {lang}. "
    "CRITICAL: Keep the labels in English exactly as they appear "
    "(e.g. 'Page Title:', 'Meta Description:', 'URL Slug:'). "
    "Only modify the text AFTER the colon. Return in the same structured format."
)

ALT_TEXT_TRANSLATOR_SYSTEM = (
    "You are an expert translator specializing in {lang}. "
    "Translate the following image alt text from English to {lang}. "
    "Preserve descriptive accuracy for accessibility. "
    "CRITICAL: Keep the labels in English exactly as they appear (e.g. 'IMG_1:', 'IMG_2:'). "
    "Only translate the text AFTER the colon. Return in the same structured format."
)

ALT_TEXT_COPYWRITER_SYSTEM = (
    "You are a native {lang} copywriter specializing in accessible content. "
    "Polish these translated image alt texts to sound natural in {lang} "
    "while remaining descriptive and accessible. "
    "CRITICAL: Keep the labels in English exactly as they appear (e.g. 'IMG_1:', 'IMG_2:'). "
    "Only modify the text AFTER the colon. Return in the same structured format."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_safe_path(base_dir: Path, name: str, lang: str, ext: str = ".xlsx") -> Path:
    """Return a file path that doesn't overwrite existing files."""
    path = base_dir / f"{name} - {lang}{ext}"
    if not path.exists():
        return path
    i = 1
    while True:
        path = base_dir / f"{name} - {lang} ({i}){ext}"
        if not path.exists():
            return path
        i += 1


def derive_page_name(url: str, soup: BeautifulSoup) -> str:
    """Derive a human-readable page name from the URL or H1."""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        # Use first few words of H1, cleaned up
        text = h1.get_text(strip=True)
        # Truncate to something reasonable for a filename
        words = text.split()[:6]
        name = " ".join(words)
    else:
        # Fallback to URL slug
        path = urlparse(url).path.rstrip("/")
        name = path.split("/")[-1] if path else "homepage"
        name = name.replace("-", " ").title()
    # Clean for filesystem
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def scrape_page(url: str) -> dict:
    """Scrape a Paperturn page and return structured content."""
    print(f"  Scraping {url} ...")
    resp = requests.get(url, timeout=30, headers={"User-Agent": "PaperturnTranslator/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # --- Page metadata ---
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta_desc_tag["content"].strip() if meta_desc_tag and meta_desc_tag.get("content") else ""

    # --- Main content (skip nav, header, footer) ---
    # Remove nav, header, footer elements
    for tag in soup.find_all(["nav", "header", "footer"]):
        tag.decompose()
    # Also remove common nav/footer classes
    for cls in ["navbar", "nav-wrapper", "footer", "site-footer", "site-header"]:
        for tag in soup.find_all(class_=re.compile(cls, re.I)):
            tag.decompose()

    # Extract content items
    content_items = []
    content_items.append(("Title", title))
    content_items.append(("Meta Description", meta_desc))

    # Find main content area
    main = soup.find("main") or soup.find(role="main") or soup.find("body")
    if not main:
        main = soup

    # Walk the DOM in order to capture everything in page sequence
    from bs4 import NavigableString

    tag_map = {
        "h1": "H1", "h2": "H2", "h3": "H3", "h4": "H4", "h5": "H5", "h6": "H6",
        "p": "P",
    }
    cta_keywords = {"trial", "demo", "start", "book", "contact", "sign up", "get started", "free"}
    cta_hrefs = {"free-trial", "demo", "sign-up", "contact", "book-a-demo", "get-started"}
    trust_keywords = {"credit card", "cancel anytime", "trusted by", "no commitment",
                      "free trial", "in-trial support", "guarantee", "money back",
                      "organizations worldwide", "businesses"}

    seen_texts = set()

    for element in main.descendants:
        # --- Bare text nodes (trust signals like "Dedicated In-Trial Support ...") ---
        if isinstance(element, NavigableString):
            text = element.strip()
            if not text or len(text) < 15 or len(text) > 200:
                continue
            if text in seen_texts:
                continue
            if any(kw in text.lower() for kw in trust_keywords):
                seen_texts.add(text)
                content_items.append(("Trust Signal", text))
            continue

        if not hasattr(element, "name"):
            continue

        text = element.get_text(strip=True)
        if not text or len(text) <= 1:
            continue

        # --- Headings and paragraphs ---
        if element.name in tag_map:
            tag_label = tag_map[element.name]
            if tag_label == "P" and len(text) < 5:
                continue
            # Skip P tags that only contain CTA links
            if tag_label == "P":
                child_links = element.find_all("a")
                link_text = "".join(a.get_text(strip=True) for a in child_links)
                if link_text and link_text == text:
                    continue
            if text not in seen_texts:
                seen_texts.add(text)
                content_items.append((tag_label, text))

        # --- CTA buttons / links ---
        elif element.name in ("button", "a"):
            if len(text) >= 100:
                continue
            classes = " ".join(element.get("class", []))
            href = element.get("href", "")
            is_cta = (
                element.name == "button"
                or "btn" in classes.lower()
                or "cta" in classes.lower()
                or any(kw in text.lower() for kw in cta_keywords)
                or any(kw in href.lower() for kw in cta_hrefs)
            )
            if is_cta and text not in seen_texts:
                seen_texts.add(text)
                content_items.append(("CTA Button", text))

        # --- Trust signal divs (leaf divs with trust keywords) ---
        elif element.name == "div":
            if len(text) < 15 or len(text) > 200:
                continue
            if len(element.find_all("div")) > 1:
                continue
            if text in seen_texts:
                continue
            if any(kw in text.lower() for kw in trust_keywords):
                seen_texts.add(text)
                content_items.append(("Trust Signal", text))

    # --- Images ---
    images = []
    for img in main.find_all("img"):
        src = img.get("src", "")
        if src and not src.startswith("data:"):
            src = urljoin(url, src)
        alt = img.get("alt", "").strip()
        if alt:  # Only include images that have alt text
            images.append((src, alt))

    # --- URL slug ---
    parsed = urlparse(url)
    slug = parsed.path.rstrip("/")

    page_name = derive_page_name(url, soup if soup.find("h1") else BeautifulSoup(resp.text, "lxml"))

    return {
        "url": url,
        "page_name": page_name,
        "content": content_items,
        "images": images,
        "seo": {
            "title": title,
            "meta_description": meta_desc,
            "slug": slug,
        },
    }


def discover_urls(subpath: str = None, crawl_all: bool = False) -> list:
    """Discover page URLs from sitemap or by crawling."""
    urls = set()

    # Try sitemap first
    sitemap_url = f"{BASE_URL}/sitemap.xml"
    try:
        resp = requests.get(sitemap_url, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml-xml")
            for loc in soup.find_all("loc"):
                url = loc.get_text(strip=True)
                if subpath and subpath not in urlparse(url).path:
                    continue
                if not crawl_all and not subpath:
                    continue
                urls.add(url)
    except Exception as e:
        print(f"  Warning: Could not fetch sitemap: {e}")

    # If sitemap didn't yield results, crawl the base page
    if not urls:
        crawl_url = f"{BASE_URL}{subpath}" if subpath else BASE_URL
        try:
            resp = requests.get(crawl_url, timeout=15)
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                full = urljoin(BASE_URL, href)
                parsed = urlparse(full)
                if parsed.netloc and "paperturn.com" in parsed.netloc:
                    if subpath and subpath not in parsed.path:
                        continue
                    if parsed.path and parsed.path != "/":
                        urls.add(full)
        except Exception as e:
            print(f"  Warning: Could not crawl {crawl_url}: {e}")

    return sorted(urls)


# ---------------------------------------------------------------------------
# Claude API Translation Pipeline
# ---------------------------------------------------------------------------
def call_claude(client: anthropic.Anthropic, system: str, user_msg: str) -> str:
    """Make a single Claude API call and return the response text."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text


def get_system_prompts(lang: str) -> dict:
    """Get all system prompts with the target language filled in."""
    return {
        "translator": TRANSLATOR_SYSTEM.format(lang=lang),
        "copywriter": COPYWRITER_SYSTEM.format(lang=lang),
        "seo_translator": SEO_TRANSLATOR_SYSTEM.format(lang=lang),
        "seo_copywriter": SEO_COPYWRITER_SYSTEM.format(lang=lang),
        "alt_translator": ALT_TEXT_TRANSLATOR_SYSTEM.format(lang=lang),
        "alt_copywriter": ALT_TEXT_COPYWRITER_SYSTEM.format(lang=lang),
    }


def format_content_for_api(items: list) -> str:
    """Format content items as 'TAG: text' lines for the API."""
    lines = []
    for tag, text in items:
        lines.append(f"{tag}: {text}")
    return "\n".join(lines)


def build_char_limit_instruction(content_items: list, char_limit_pct: int) -> str:
    """Build a character limit instruction string for the copywriter prompt."""
    lines = ["CHARACTER LIMITS: Each line must not exceed the max characters shown below."]
    for tag, text in content_items:
        max_chars = int(len(text) * (1 + char_limit_pct / 100))
        lines.append(f"  {tag}: max {max_chars} characters")
    lines.append("If the translation is too long, rephrase to fit. Do not truncate mid-sentence.")
    return "\n".join(lines)


def parse_api_response(response: str, expected_tags: list) -> list:
    """Parse 'TAG: text' response back into list of (tag, text) tuples."""
    results = []
    lines = response.strip().split("\n")

    # Sort tags by length descending so longer tags match first (e.g., "Page Title" before "P")
    sorted_tags = sorted(expected_tags, key=len, reverse=True)

    # Build a buffer for multi-line values
    current_tag = None
    current_text = []

    for line in lines:
        # Try to match a tag prefix
        matched = False
        for tag in sorted_tags:
            prefix = f"{tag}:"
            if line.startswith(prefix):
                # Save previous
                if current_tag is not None:
                    results.append((current_tag, " ".join(current_text).strip()))
                current_tag = tag
                current_text = [line[len(prefix):].strip()]
                matched = True
                break
        if not matched and current_tag is not None:
            current_text.append(line.strip())

    # Don't forget the last one
    if current_tag is not None:
        results.append((current_tag, " ".join(current_text).strip()))

    return results


def translate_content(client: anthropic.Anthropic, content_items: list,
                      translator_prompt: str, copywriter_prompt: str,
                      lang: str, char_limit_pct: int = None) -> tuple:
    """Two-step translate + copywrite pipeline. Returns (translations, final_copies)."""
    if not content_items:
        return [], []

    expected_tags = [tag for tag, _ in content_items]
    formatted = format_content_for_api(content_items)

    # Step 1: Translate
    print(f"    Translating content ({len(content_items)} items) ...")
    translation_raw = call_claude(client, translator_prompt, formatted)
    translations = parse_api_response(translation_raw, expected_tags)

    # Step 2: Copywrite (fresh call)
    print(f"    Copywriting content ...")
    cw_prompt = copywriter_prompt
    user_msg = translation_raw
    if char_limit_pct is not None:
        char_instructions = build_char_limit_instruction(content_items, char_limit_pct)
        user_msg = f"{translation_raw}\n\n{char_instructions}"
    final_raw = call_claude(client, cw_prompt, user_msg)
    final_copies = parse_api_response(final_raw, expected_tags)

    return translations, final_copies


def translate_images(client: anthropic.Anthropic, images: list,
                     translator_prompt: str, copywriter_prompt: str,
                     lang: str, char_limit_pct: int = None) -> tuple:
    """Translate image alt text through the two-step pipeline."""
    if not images:
        return [], []

    # Format as numbered items
    items = [(f"IMG_{i+1}", alt) for i, (_, alt) in enumerate(images)]
    expected_tags = [tag for tag, _ in items]
    formatted = format_content_for_api(items)

    print(f"    Translating {len(images)} image alt texts ...")
    translation_raw = call_claude(client, translator_prompt, formatted)
    translations = parse_api_response(translation_raw, expected_tags)

    print(f"    Copywriting image alt texts ...")
    user_msg = translation_raw
    if char_limit_pct is not None:
        char_instructions = build_char_limit_instruction(items, char_limit_pct)
        user_msg = f"{translation_raw}\n\n{char_instructions}"
    final_raw = call_claude(client, copywriter_prompt, user_msg)
    final_copies = parse_api_response(final_raw, expected_tags)

    return translations, final_copies


def translate_seo(client: anthropic.Anthropic, seo: dict,
                  translator_prompt: str, copywriter_prompt: str,
                  lang: str, char_limit_pct: int = None) -> tuple:
    """Translate SEO metadata through the two-step pipeline."""
    items = [
        ("Page Title", seo["title"]),
        ("Meta Description", seo["meta_description"]),
        ("URL Slug", seo["slug"]),
    ]
    expected_tags = [tag for tag, _ in items]
    formatted = format_content_for_api(items)

    print(f"    Translating SEO metadata ...")
    translation_raw = call_claude(client, translator_prompt, formatted)
    translations = parse_api_response(translation_raw, expected_tags)

    print(f"    Copywriting SEO metadata ...")
    user_msg = translation_raw
    if char_limit_pct is not None:
        char_instructions = build_char_limit_instruction(items, char_limit_pct)
        user_msg = f"{translation_raw}\n\n{char_instructions}"
    final_raw = call_claude(client, copywriter_prompt, user_msg)
    final_copies = parse_api_response(final_raw, expected_tags)

    return translations, final_copies


# ---------------------------------------------------------------------------
# SEMrush Integration (optional)
# ---------------------------------------------------------------------------
def fetch_semrush_keywords(api_key: str, url: str, lang: str) -> list:
    """Fetch keyword recommendations from SEMrush API."""
    # Map language names to SEMrush database codes
    lang_db_map = {
        "spanish": "es", "french": "fr", "german": "de",
        "danish": "dk", "swedish": "se", "italian": "it",
        "portuguese": "br", "dutch": "nl",
    }
    db = lang_db_map.get(lang.lower(), "us")
    domain = urlparse(url).netloc

    try:
        # Get organic keywords for the domain in target market
        api_url = (
            f"https://api.semrush.com/"
            f"?type=url_organic"
            f"&key={api_key}"
            f"&url={url}"
            f"&database={db}"
            f"&display_limit=20"
            f"&export_columns=Ph,Po,Nq,Cp"
        )
        resp = requests.get(api_url, timeout=30)
        if resp.status_code == 200 and resp.text.strip():
            keywords = []
            reader = csv.reader(resp.text.strip().split("\n"), delimiter=";")
            next(reader, None)  # Skip header
            for row in reader:
                if len(row) >= 2:
                    keywords.append({"keyword": row[0], "position": row[1],
                                     "volume": row[2] if len(row) > 2 else "",
                                     "cpc": row[3] if len(row) > 3 else ""})
            return keywords
        else:
            print(f"    SEMrush returned status {resp.status_code}")
            return []
    except Exception as e:
        print(f"    SEMrush error: {e}")
        return []


# ---------------------------------------------------------------------------
# Excel Output
# ---------------------------------------------------------------------------
HEADER_FONT = Font(name="Calibri", bold=True, size=11)
HEADER_FILL = PatternFill(start_color="117681", end_color="117681", fill_type="solid")
HEADER_FONT_WHITE = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
WRAP = Alignment(wrap_text=True, vertical="top")


def style_header(ws, num_cols):
    """Apply header styling to the first row."""
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT_WHITE
        cell.fill = HEADER_FILL
        cell.alignment = WRAP


def write_xlsx(output_path: Path, page_data: dict, content_translations: tuple,
               image_translations: tuple, seo_translations: tuple,
               semrush_keywords: list = None):
    """Write all translation data to an Excel workbook."""
    wb = Workbook()

    # --- Sheet 1: Content ---
    ws_content = wb.active
    ws_content.title = "Content"
    ws_content.append(["Text Tag", "English", "Translation", "Final Copy"])
    style_header(ws_content, 4)

    content_items = page_data["content"]
    translations, final_copies = content_translations

    for i, (tag, english) in enumerate(content_items):
        trans_text = translations[i][1] if i < len(translations) else ""
        final_text = final_copies[i][1] if i < len(final_copies) else ""
        ws_content.append([tag, english, trans_text, final_text])

    # Auto-width columns
    for col_letter in ["A", "B", "C", "D"]:
        ws_content.column_dimensions[col_letter].width = 40
    ws_content.column_dimensions["A"].width = 18

    # --- Sheet 2: Images ---
    ws_images = wb.create_sheet("Images")
    ws_images.append(["Image URL", "English Alt Text", "Translation", "Final Copy"])
    style_header(ws_images, 4)

    images = page_data["images"]
    img_translations, img_final = image_translations

    for i, (src, alt) in enumerate(images):
        trans_text = img_translations[i][1] if i < len(img_translations) else ""
        final_text = img_final[i][1] if i < len(img_final) else ""
        ws_images.append([src, alt, trans_text, final_text])

    for col_letter in ["A", "B", "C", "D"]:
        ws_images.column_dimensions[col_letter].width = 40

    # --- Sheet 3: SEO ---
    ws_seo = wb.create_sheet("SEO")
    ws_seo.append(["Field", "English", "Translation", "Final Copy"])
    style_header(ws_seo, 4)

    seo = page_data["seo"]
    seo_items = [
        ("Page Title", seo["title"]),
        ("Meta Description", seo["meta_description"]),
        ("URL Slug", seo["slug"]),
    ]
    seo_trans, seo_final = seo_translations

    for i, (field, english) in enumerate(seo_items):
        trans_text = seo_trans[i][1] if i < len(seo_trans) else ""
        final_text = seo_final[i][1] if i < len(seo_final) else ""
        ws_seo.append([field, english, trans_text, final_text])

    # SEMrush keywords row
    if semrush_keywords:
        kw_str = ", ".join(
            f"{kw['keyword']} (vol:{kw['volume']}, pos:{kw['position']})"
            for kw in semrush_keywords
        )
        ws_seo.append(["SEMrush Keywords", "—", kw_str, "—"])
    else:
        ws_seo.append(["SEMrush Keywords", "—", "—", "—"])

    for col_letter in ["A", "B", "C", "D"]:
        ws_seo.column_dimensions[col_letter].width = 45
    ws_seo.column_dimensions["A"].width = 20

    # Save
    wb.save(output_path)
    print(f"  Saved: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Batch translate Paperturn pages into target languages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python translate.py --lang Spanish --urls urls.txt
  python translate.py --lang French --subpath /industries/
  python translate.py --lang German --all
        """,
    )
    parser.add_argument("--lang", required=True, help="Target language (e.g., Spanish, French)")
    parser.add_argument("--urls", help="Path to text file with one URL per line")
    parser.add_argument("--subpath", help="Crawl pages under a subpath (e.g., /industries/)")
    parser.add_argument("--all", action="store_true", help="Crawl entire site")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory")
    parser.add_argument("--semrush", action="store_true", help="Enable SEMrush keyword analysis (prompts per URL)")
    parser.add_argument("--char-limit", type=int, default=None, metavar="PCT",
                        help="Enforce character limit on final copy (e.g., 10 = allow 10%% overshoot vs English)")

    args = parser.parse_args()

    # Validate input mode
    if not args.urls and not args.subpath and not args.all:
        parser.error("Provide one of: --urls <file>, --subpath <path>, or --all")

    # Check API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.")
        print("  export ANTHROPIC_API_KEY='your-key-here'")
        sys.exit(1)

    semrush_key = os.environ.get("SEMRUSH_API_KEY") if args.semrush else None
    if args.semrush and not semrush_key:
        print("Error: SEMRUSH_API_KEY environment variable not set.")
        sys.exit(1)

    # Gather URLs
    urls = []
    if args.urls:
        url_file = Path(args.urls)
        if not url_file.exists():
            print(f"Error: URL file not found: {args.urls}")
            sys.exit(1)
        urls = [line.strip() for line in url_file.read_text().splitlines() if line.strip() and not line.startswith("#")]
    elif args.subpath or args.all:
        print(f"Discovering URLs {'for ' + args.subpath if args.subpath else '(full site)'}...")
        urls = discover_urls(subpath=args.subpath, crawl_all=args.all)

    if not urls:
        print("No URLs found to process.")
        sys.exit(1)

    print(f"\nFound {len(urls)} page(s) to translate into {args.lang}:\n")
    for u in urls:
        print(f"  - {u}")
    print()

    # Setup
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic(api_key=api_key)

    # Process each page
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] Processing: {url}")

        # 1. Scrape
        try:
            page_data = scrape_page(url)
        except Exception as e:
            print(f"  Error scraping {url}: {e}")
            continue

        # 2. Get system prompts for this page (fresh per page)
        prompts = get_system_prompts(args.lang)

        # 3. Content translation + copywriting
        try:
            content_translations = translate_content(
                client, page_data["content"],
                prompts["translator"],
                prompts["copywriter"],
                args.lang,
                char_limit_pct=args.char_limit,
            )
        except Exception as e:
            print(f"  Error translating content: {e}")
            content_translations = ([], [])

        # 4. Image alt text translation + copywriting
        try:
            image_translations = translate_images(
                client, page_data["images"],
                prompts["alt_translator"],
                prompts["alt_copywriter"],
                args.lang,
                char_limit_pct=args.char_limit,
            )
        except Exception as e:
            print(f"  Error translating images: {e}")
            image_translations = ([], [])

        # 5. SEO translation + copywriting
        try:
            seo_translations = translate_seo(
                client, page_data["seo"],
                prompts["seo_translator"],
                prompts["seo_copywriter"],
                args.lang,
                char_limit_pct=args.char_limit,
            )
        except Exception as e:
            print(f"  Error translating SEO: {e}")
            seo_translations = ([], [])

        # 6. SEMrush (optional, gated)
        semrush_keywords = []
        if args.semrush and semrush_key:
            confirm = input(f"\n  Run SEMrush analysis for {url}? [y/N]: ").strip().lower()
            if confirm == "y":
                print(f"    Fetching SEMrush keywords ...")
                semrush_keywords = fetch_semrush_keywords(semrush_key, url, args.lang)
                print(f"    Found {len(semrush_keywords)} keywords")

        # 7. Write Excel
        file_path = get_safe_path(output_dir, page_data["page_name"], args.lang)
        try:
            write_xlsx(file_path, page_data, content_translations,
                       image_translations, seo_translations, semrush_keywords)
        except Exception as e:
            print(f"  Error writing Excel: {e}")

    print(f"\nDone! {len(urls)} page(s) processed. Output: {output_dir}")


if __name__ == "__main__":
    main()
