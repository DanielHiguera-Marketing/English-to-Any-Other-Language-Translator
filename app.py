#!/usr/bin/env python3
"""
Paperturn Translation App
Streamlit front-end for the batch translation script.
"""

import io
import os
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import streamlit as st

from translate import (
    scrape_page,
    discover_urls,
    get_system_prompts,
    translate_content,
    translate_images,
    translate_seo,
    fetch_semrush_keywords,
    write_xlsx,
    get_safe_path,
    DEFAULT_OUTPUT,
)

try:
    import anthropic
except ImportError:
    st.error("Missing `anthropic` package. Run: pip install anthropic")
    st.stop()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Paperturn Translator",
    page_icon="📄",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .stApp { max-width: 1000px; margin: 0 auto; }
    div[data-testid="stStatusWidget"] { display: none; }

</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Sidebar: API Keys
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("API Keys")
    anthropic_key = st.text_input(
        "Anthropic API Key",
        value=os.environ.get("ANTHROPIC_API_KEY", ""),
        type="password",
        help="Required for translation. Set ANTHROPIC_API_KEY env var to pre-fill.",
    )
    semrush_key = st.text_input(
        "SEMrush API Key (optional)",
        value=os.environ.get("SEMRUSH_API_KEY", ""),
        type="password",
        help="Only needed if you enable SEMrush keyword analysis.",
    )
    is_local = os.path.isdir(os.path.expanduser("~/Desktop"))
    if is_local:
        st.divider()
        output_dir = st.text_input(
            "Output directory",
            value=str(DEFAULT_OUTPUT),
            help="Where .xlsx files are also saved locally.",
        )
    else:
        output_dir = None

# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------
st.title("Paperturn Translator")
st.caption("Scrape, translate, and copywrite Paperturn pages into any language.")

# --- Step 1: Language ---
st.subheader("1. Target Language(s)")
selected_langs = st.multiselect(
    "Which language(s) are you translating into?",
    ["Spanish", "French", "German", "Danish", "Swedish", "Italian", "Portuguese", "Dutch"],
    default=["Spanish"],
)
custom_lang = st.text_input("Or add a custom language:", "")
target_langs = list(selected_langs)
if custom_lang.strip() and custom_lang.strip() not in target_langs:
    target_langs.append(custom_lang.strip())
# Keep backward compat for single-lang references
target_lang = target_langs[0] if target_langs else "Spanish"

# --- Step 2: URLs ---
st.subheader("2. Pages to Translate")
url_mode = st.radio(
    "How would you like to provide URLs?",
    ["Paste URLs", "Upload a file", "Crawl a subpath", "Crawl entire site"],
    horizontal=True,
)

urls = []

if url_mode == "Paste URLs":
    url_text = st.text_area(
        "Paste one URL per line:",
        placeholder="https://www.paperturn.com/industries/manufacturing\nhttps://www.paperturn.com/industries/real-estate",
        height=150,
    )
    urls = [u.strip() for u in url_text.strip().splitlines() if u.strip() and not u.startswith("#")]

elif url_mode == "Upload a file":
    uploaded = st.file_uploader("Upload a .txt file with one URL per line", type=["txt"])
    if uploaded:
        content = uploaded.read().decode("utf-8")
        urls = [u.strip() for u in content.splitlines() if u.strip() and not u.startswith("#")]

elif url_mode == "Crawl a subpath":
    subpath = st.text_input("Subpath to crawl:", value="/industries/", help="e.g., /industries/, /features/")
    if subpath and st.button("Discover URLs"):
        with st.spinner("Discovering pages..."):
            urls = discover_urls(subpath=subpath)
        st.session_state["discovered_urls"] = urls
    if "discovered_urls" in st.session_state:
        urls = st.session_state["discovered_urls"]

elif url_mode == "Crawl entire site":
    if st.button("Discover all URLs"):
        with st.spinner("Discovering pages..."):
            urls = discover_urls(crawl_all=True)
        st.session_state["discovered_urls"] = urls
    if "discovered_urls" in st.session_state:
        urls = st.session_state["discovered_urls"]

if urls:
    st.success(f"**{len(urls)} page(s) ready to translate:**")
    for u in urls:
        st.text(f"  {u}")

# --- Step 3: Options ---
st.subheader("3. Options")
col1, col2 = st.columns(2)
with col1:
    enable_semrush = st.checkbox("Enable SEMrush keyword analysis", value=False)
    enable_char_limit = st.checkbox("Enforce character limits", value=False,
                                     help="Keep translated copy close to the English character count per tag.")
with col2:
    if enable_semrush and urls:
        semrush_urls = st.multiselect(
            "Select URLs for SEMrush analysis:",
            urls,
            default=[],
            help="Only selected URLs will be analyzed (costs API credits).",
        )
    else:
        semrush_urls = []
    if enable_char_limit:
        char_limit_pct = st.slider(
            "Max character overshoot allowed",
            min_value=0, max_value=30, value=10, step=5,
            format="%d%%",
            help="0% = exact match, 10% = allow 10% longer than English, etc.",
        )
    else:
        char_limit_pct = None

# --- Step 4: Run ---
st.subheader("4. Translate")

can_run = bool(urls) and bool(anthropic_key) and bool(target_langs)
if not anthropic_key:
    st.warning("Enter your Anthropic API key in the sidebar to continue.")
if not target_langs:
    st.warning("Select at least one target language.")
if enable_semrush and not semrush_key:
    st.warning("Enter your SEMrush API key in the sidebar, or disable SEMrush.")


def process_page_lang(url, lang, page_data, api_key, semrush_key_val,
                      enable_semrush_flag, semrush_urls_list, char_limit_pct_val,
                      out_path, local_path):
    """Process a single page + language combo. Runs in its own thread."""
    client = anthropic.Anthropic(api_key=api_key)
    prompts = get_system_prompts(lang)

    # SEMrush
    semrush_keywords = []
    if enable_semrush_flag and semrush_key_val and url in semrush_urls_list:
        semrush_keywords = fetch_semrush_keywords(
            semrush_key_val, url, lang,
            client=client, content_items=page_data["content"],
        )

    # Content
    content_translations = translate_content(
        client, page_data["content"],
        prompts["translator"], prompts["copywriter"],
        lang, char_limit_pct=char_limit_pct_val,
        seo_keywords=semrush_keywords if semrush_keywords else None,
    )

    # Images
    try:
        image_translations = translate_images(
            client, page_data["images"],
            prompts["alt_translator"], prompts["alt_copywriter"],
            lang, char_limit_pct=char_limit_pct_val,
        )
    except Exception as e:
        print(f"  Image translation failed ({lang}): {e}")
        image_translations = ([], [])

    # SEO
    try:
        seo_translations = translate_seo(
            client, page_data["seo"],
            prompts["seo_translator"], prompts["seo_copywriter"],
            lang, char_limit_pct=char_limit_pct_val,
        )
    except Exception as e:
        print(f"  SEO translation failed ({lang}): {e}")
        seo_translations = ([], [])

    # Write Excel
    file_path = get_safe_path(out_path, page_data["page_name"], lang)
    write_xlsx(file_path, page_data, content_translations,
               image_translations, seo_translations, semrush_keywords)

    if local_path:
        local_file = get_safe_path(local_path, page_data["page_name"], lang)
        shutil.copy2(file_path, local_file)

    return file_path, lang, url


if st.button("Start Translation", disabled=not can_run, type="primary"):
    out_path = Path(tempfile.mkdtemp())
    local_path = None
    if output_dir:
        local_path = Path(output_dir)
        local_path.mkdir(parents=True, exist_ok=True)

    # Scrape all pages first (shared across languages)
    status = st.empty()
    all_page_data = {}
    for i, url in enumerate(urls):
        status.markdown(f"**Scraping [{i+1}/{len(urls)}]** `{url}` ...")
        try:
            all_page_data[url] = scrape_page(url)
        except Exception as e:
            st.error(f"Failed to scrape {url}: {e}")

    if not all_page_data:
        st.error("No pages could be scraped.")
    else:
        # Build all jobs: (url, lang) pairs
        jobs = [(url, lang) for url in all_page_data for lang in target_langs]
        total_jobs = len(jobs)

        status.markdown(f"**Translating {len(all_page_data)} page(s) into {len(target_langs)} language(s) "
                        f"({total_jobs} jobs, running in parallel) ...**")
        progress_bar = st.progress(0)
        completed_files = []
        errors = []
        done_count = 0

        # Run all (page × language) jobs in parallel, capped to avoid rate limits
        max_workers = min(total_jobs, 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for url, lang in jobs:
                future = executor.submit(
                    process_page_lang,
                    url, lang, all_page_data[url], anthropic_key,
                    semrush_key, enable_semrush, semrush_urls, char_limit_pct,
                    out_path, local_path,
                )
                futures[future] = (url, lang)

            for future in as_completed(futures):
                url, lang = futures[future]
                done_count += 1
                try:
                    file_path, lang_done, url_done = future.result()
                    completed_files.append(file_path)
                    status.markdown(f"**[{done_count}/{total_jobs}]** Completed: `{url}` → {lang}")
                except Exception as e:
                    errors.append(f"{url} ({lang}): {e}")
                    st.error(f"Failed: {url} ({lang}): {e}")
                progress_bar.progress(done_count / total_jobs)

        status.empty()
        progress_bar.empty()

        if completed_files:
            st.balloons()
            file_data = []
            for f in completed_files:
                with open(f, "rb") as fh:
                    file_data.append({"name": f.name, "data": fh.read()})
            st.session_state["completed_files"] = file_data
            if local_path:
                st.session_state["local_path"] = str(local_path)
        else:
            st.error("No files were generated. Check the errors above.")

# Show download buttons from session state (persists across reruns)
if "completed_files" in st.session_state and st.session_state["completed_files"]:
    files = st.session_state["completed_files"]
    local = st.session_state.get("local_path")

    st.divider()
    if local:
        st.success(f"**{len(files)} file(s)** saved to `{local}` and ready to download.")
    else:
        st.success(f"**{len(files)} file(s)** ready to download.")

    # Download All as zip (if more than one file)
    if len(files) > 1:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.writestr(f["name"], f["data"])
        zip_buffer.seek(0)
        st.download_button(
            label=f"Download All ({len(files)} files as .zip)",
            data=zip_buffer.getvalue(),
            file_name=f"translations-{target_lang.lower()}.zip",
            mime="application/zip",
            key="download_all",
        )

    # Individual download buttons
    for i, f in enumerate(files):
        st.download_button(
            label=f"Download {f['name']}",
            data=f["data"],
            file_name=f["name"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"download_{i}",
        )
